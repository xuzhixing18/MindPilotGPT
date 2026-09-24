"""轻量声明式迁移：启动时把 ORM 元数据与实际库结构对齐（幂等，不引入 Alembic）。

为什么需要：``Base.metadata.create_all`` **只建缺失的表**——既不会给已存在的表加列，
也不会放宽列约束。老库（如已在用的 ``data/mindpilot.db``）在新模型上线后会直接
``no such column: users.phone``。阶段A 单机 SQLite 引入 Alembic 成本过高，故用本模块做
「够用且安全」的自动对齐；阶段1 迁 Postgres 后可整体替换为 Alembic，业务代码不受影响。

对齐三类差异（按此顺序执行）：
1. **缺列** → ``ALTER TABLE ADD COLUMN``（SQLite/Postgres/MySQL 通用）；
   注意 ``CreateColumn`` 只渲染列本身，``unique=True`` 在 SQLAlchemy 中合并进唯一索引，
   故唯一性由第 3 步的索引同步保证；
2. **列约束放宽**（如 ``NOT NULL → NULL``，SQLite 无 ``ALTER COLUMN``）→ 重建该表：
   旧表改名 → 删其索引 → 按 ORM 建新表 → 拷同名列数据 → 数据修正 → 建索引 → 删旧表；
   索引必须最后建：否则数据修正（如空串归一为 NULL）会撞唯一约束；
3. **缺索引** → 补建 ORM 中声明而库里缺失的索引（承载 UNIQUE）。

安全约束：
- 幂等：先探测后动手，重复执行无副作用；
- 只加不减：绝不删列、不改类型、不丢数据；无法自动处理的差异只告警跳过；
- 重建前对文件型 SQLite 备份为 ``*.pre-migration.bak``（已存在则不覆盖）；
- 单表重建在一个事务内，失败整体回滚，不留半成品表；
- 迁移失败不抛出（延续 ``init_db`` 的「不拖垮启动」约定），但会以 ERROR 级日志暴露，
  并把动作记入 ``APPLIED`` 供 /api/health 与测试观测。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from sqlalchemy import Table, inspect, text
from sqlalchemy.schema import CreateColumn, CreateIndex, CreateTable

from backend.storage.db import DATABASE_URL, Base, engine

log = logging.getLogger(__name__)

# 最近一次 ensure_schema() 执行的动作（供测试断言与 /api/health 观测）
APPLIED: list[str] = []

# 结构对齐后的一次性数据修正：幂等（WHERE 精确限定），按表名归组。
# 在「建新表 + 拷数据」之后、「建索引」之前执行，故不会撞唯一约束。
_DATA_FIXUPS: dict[str, tuple[str, ...]] = {
    # 旧版 users.email 为 NOT NULL DEFAULT ''；改可空后空串须归一为 NULL，
    # 否则多个「只有手机号」的用户会在唯一索引上互相冲突。
    "users": ("UPDATE users SET email = NULL WHERE email = ''",),
}


def _reflected_columns(table_name: str) -> dict[str, dict]:
    """库中该表的列信息，按列名索引（含 nullable / default / type）。"""
    return {c["name"]: c for c in inspect(engine).get_columns(table_name)}


def _columns_to_relax(existing: dict[str, dict], table: Table) -> list[str]:
    """库中 NOT NULL 而 ORM 允许 NULL 的列（需要重建表才能放宽）。"""
    return [
        col.name
        for col in table.columns
        if col.name in existing and existing[col.name].get("nullable") is False and col.nullable
    ]


def _columns_too_strict(existing: dict[str, dict], table: Table) -> list[str]:
    """库中可空而 ORM 要求 NOT NULL 的列：无法自动收紧（可能已有 NULL 数据），只告警。"""
    return [
        col.name
        for col in table.columns
        if col.name in existing and existing[col.name].get("nullable") is True and not col.nullable
    ]


def _backup_sqlite_file() -> None:
    """文件型 SQLite：重建前备份一份，供意外回滚（已存在备份则不覆盖）。"""
    if not DATABASE_URL.startswith("sqlite") or ":memory:" in DATABASE_URL:
        return
    try:
        src = Path(DATABASE_URL.split("sqlite:///", 1)[-1])
        if src.is_file():
            bak = src.with_name(src.name + ".pre-migration.bak")
            if not bak.exists():
                shutil.copy2(src, bak)
                log.warning("[migrations] 已备份数据库文件 -> %s", bak)
    except Exception as exc:  # 备份失败不阻断迁移，但要可见
        log.warning("[migrations] 备份数据库失败（继续执行）：%s", exc)


def _add_missing_columns(table_name: str, table: Table, existing: dict[str, dict]) -> list[str]:
    """补齐 ORM 有而库里缺的列。新列必须可空或带 server_default，否则 SQLite 拒绝。"""
    actions: list[str] = []
    for col in table.columns:
        if col.name in existing:
            continue
        ddl = str(CreateColumn(col).compile(dialect=engine.dialect))
        try:
            with engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE "{table_name}" ADD COLUMN {ddl}'))
            actions.append(f"{table_name}.{col.name}: added")
            log.info("[migrations] %s 新增列 %s", table_name, ddl)
        except Exception as exc:
            log.error("[migrations] %s 加列 %s 失败：%s", table_name, col.name, exc)
    return actions


def _sync_indexes(table_name: str, table: Table) -> list[str]:
    """补建 ORM 声明而库中缺失的索引（UNIQUE 由唯一索引承载，不可漏）。"""
    actions: list[str] = []
    have = {ix["name"] for ix in inspect(engine).get_indexes(table_name) if ix.get("name")}
    for ix in sorted(table.indexes, key=lambda i: str(i.name)):
        if ix.name in have:
            continue
        try:
            CreateIndex(ix).execute(engine)
            actions.append(f"{table_name}.{ix.name}: index created")
            log.info("[migrations] %s 补建索引 %s (unique=%s)", table_name, ix.name, ix.unique)
        except Exception as exc:
            # 数据已有重复值时唯一索引建不起来；启动不能死，但必须高声暴露
            log.error("[migrations] %s 补建索引 %s 失败（唯一性可能未生效）：%s", table_name, ix.name, exc)
    return actions


def _rebuild_table(table_name: str, table: Table, existing: dict[str, dict], relaxed: list[str]) -> str:
    """SQLite 重建表以放宽列约束：改名 → 建新表 → 拷数据 → 修正 → 建索引 → 删旧表。

    全程单事务，失败自动回滚（SQLite 的 DDL 是事务性的）。
    索引名不随 ``ALTER TABLE RENAME`` 改变，故须先显式删除旧索引，否则新表建同名索引冲突。
    """
    old_indexes = [
        ix["name"] for ix in inspect(engine).get_indexes(table_name) if ix.get("name")
    ]
    common = [c.name for c in table.columns if c.name in existing]
    if not common:
        raise RuntimeError(f"{table_name}: 新旧表无同名列，拒绝重建")
    cols = ", ".join(f'"{c}"' for c in common)
    tmp = f"{table_name}__mig_old"
    fixups = _DATA_FIXUPS.get(table_name, ())

    _backup_sqlite_file()
    with engine.begin() as conn:
        conn.execute(text(f'ALTER TABLE "{table_name}" RENAME TO "{tmp}"'))
        for name in old_indexes:
            conn.execute(text(f'DROP INDEX IF EXISTS "{name}"'))
        conn.execute(CreateTable(table))  # 只建表，索引留到最后
        conn.execute(text(f'INSERT INTO "{table_name}" ({cols}) SELECT {cols} FROM "{tmp}"'))
        for stmt in fixups:
            conn.execute(text(stmt))
        for ix in sorted(table.indexes, key=lambda i: str(i.name)):
            conn.execute(CreateIndex(ix))
        conn.execute(text(f'DROP TABLE "{tmp}"'))

    log.warning(
        "[migrations] %s 表已重建以放宽约束 %s（拷贝 %d 列，数据修正 %d 条）",
        table_name, relaxed, len(common), len(fixups),
    )
    return f"{table_name}: rebuilt (relaxed={','.join(relaxed)}, columns={len(common)})"


def ensure_schema() -> list[str]:
    """对齐 ORM 与实际库结构，返回本次执行的动作（空列表表示无需变更）。幂等。"""
    global APPLIED
    # 延迟导入，确保表已注册进 Base.metadata（与 init_db 同样处理，避免循环导入）
    from backend.storage import models  # noqa: F401

    actions: list[str] = []
    for table_name, table in Base.metadata.tables.items():
        if not inspect(engine).has_table(table_name):
            continue  # 建表交给 create_all
        existing = _reflected_columns(table_name)

        orphan = sorted(set(existing) - {c.name for c in table.columns})
        if orphan:
            log.warning("[migrations] %s 存在 ORM 未声明的列 %s（保留不动）", table_name, orphan)

        actions.extend(_add_missing_columns(table_name, table, existing))

        relaxed = _columns_to_relax(existing, table)
        if relaxed:
            if engine.dialect.name == "sqlite":
                try:
                    actions.append(_rebuild_table(table_name, table, existing, relaxed))
                except Exception as exc:
                    log.error("[migrations] %s 重建失败（结构可能未对齐）：%s", table_name, exc)
            elif engine.dialect.name == "postgresql":
                for name in relaxed:
                    with engine.begin() as conn:
                        conn.execute(text(
                            f'ALTER TABLE "{table_name}" ALTER COLUMN "{name}" DROP NOT NULL'
                        ))
                    actions.append(f"{table_name}.{name}: drop not null")
            else:
                log.warning(
                    "[migrations] %s.%s 需放宽 NOT NULL，当前方言 %s 未自动处理，请人工执行 DDL",
                    table_name, relaxed, engine.dialect.name,
                )

        strict = _columns_too_strict(existing, table)
        if strict:
            log.warning(
                "[migrations] %s 的列 %s 在库中可空但 ORM 要求 NOT NULL；"
                "无法自动收紧（可能已有 NULL 数据），请人工核查", table_name, strict,
            )

        actions.extend(_sync_indexes(table_name, table))

    APPLIED = actions
    return actions
