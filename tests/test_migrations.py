"""轻量声明式迁移（storage.migrations）的离线测试。

为什么单独一个文件：迁移必须在**旧结构的库**上验证，而其余测试都跑在新建库上
（结构天然对齐，走不到迁移分支）。本用例先用裸 SQL 造出改造前的 users 表
（13 列、email NOT NULL、仅 ix_users_email），再让 ``init_db()`` 把它对齐到当前 ORM。

覆盖：
- 缺列补齐（8 个新列）与约束放宽（email NOT NULL → NULL，SQLite 走重建表）
- 唯一索引同步（ix_users_email / ix_users_phone 均 unique）——这是手机号唯一性的唯一承载
- 数据零丢失 + 一次性数据修正（email='' → NULL，否则多个纯手机号账号会撞唯一索引）
- 幂等（二次执行 APPLIED 为空）与重建前自动备份 *.pre-migration.bak

运行：python tests/test_migrations.py
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

# 将项目根目录加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP_DIR = Path(tempfile.mkdtemp(prefix="mp_mig_test_"))
_DB_PATH = _TMP_DIR / "legacy.db"

# 关键：db.py 在「导入时」即按 DATABASE_URL 建 engine，必须先于任何 backend.* 导入设置
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH.as_posix()}"

# 改造前的 users 表（仅登录标识符为邮箱、无个人资料字段）
_LEGACY_DDL = """
CREATE TABLE users (
    id VARCHAR(32) NOT NULL,
    email VARCHAR(255) NOT NULL DEFAULT '',
    password_hash VARCHAR(255) NOT NULL,
    nickname TEXT NOT NULL,
    status VARCHAR(16) NOT NULL,
    email_verified BOOLEAN NOT NULL,
    plan_id VARCHAR(32) NOT NULL,
    org_id VARCHAR(32),
    privacy_mode BOOLEAN NOT NULL,
    failed_login_count INTEGER NOT NULL,
    locked_until DATETIME,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id)
);
CREATE UNIQUE INDEX ix_users_email ON users (email);
"""

_NEW_COLUMNS = {
    "phone", "avatar_url", "bio", "gender", "birthday", "location", "website", "phone_verified",
    "ai_provider", "ai_model",
}


def _make_legacy_db() -> None:
    """造一个改造前的库：两行数据，其中一行 email 为空串（旧版默认值）。"""
    conn = sqlite3.connect(_DB_PATH)
    try:
        conn.executescript(_LEGACY_DDL)
        now = "2026-01-01 00:00:00"
        conn.executemany(
            "INSERT INTO users (id, email, password_hash, nickname, status, email_verified,"
            " plan_id, org_id, privacy_mode, failed_login_count, locked_until, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("a" * 32, "legacy@example.com", "pbkdf2_sha256$x", "老用户", "active",
                 0, "free", None, 0, 0, None, now, now),
                # email='' 是旧版「无邮箱」的写法，迁移后必须归一为 NULL
                ("b" * 32, "", "pbkdf2_sha256$y", "空邮箱用户", "active",
                 0, "free", None, 0, 0, None, now, now),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _columns() -> dict[str, dict]:
    conn = sqlite3.connect(_DB_PATH)
    try:
        rows = conn.execute("PRAGMA table_info(users)").fetchall()
        return {r[1]: {"nullable": not r[3], "default": r[4]} for r in rows}
    finally:
        conn.close()


def _indexes() -> dict[str, bool]:
    """显式索引名 → 是否唯一（主键自带的 sqlite_autoindex_* 不算，SQLAlchemy 也不管它）。"""
    conn = sqlite3.connect(_DB_PATH)
    try:
        rows = conn.execute("PRAGMA index_list(users)").fetchall()
        return {r[1]: bool(r[2]) for r in rows if not r[1].startswith("sqlite_autoindex_")}
    finally:
        conn.close()


def test_legacy_shape_before_migration():
    """前置断言：确认造的确实是「旧结构」，否则后面的迁移断言没有意义。"""
    _make_legacy_db()
    cols = _columns()
    assert len(cols) == 13, cols
    assert cols["email"]["nullable"] is False          # 旧版 email 是 NOT NULL
    assert not (_NEW_COLUMNS & set(cols)), "旧库不应含新列"
    assert _indexes() == {"ix_users_email": True}
    print("[mig] legacy db shape ok（13 列 / email NOT NULL / 仅邮箱唯一索引）")


def test_ensure_schema_aligns_structure():
    from backend import storage
    from backend.storage import migrations

    assert storage.init_db() is True
    applied = migrations.APPLIED
    assert applied, f"首次迁移应有动作，实际为空：{applied}"
    assert any("users: rebuilt" in a for a in applied), applied   # email 放宽须走重建

    cols = _columns()
    missing = _NEW_COLUMNS - set(cols)
    assert not missing, f"新列未补齐：{missing}"
    assert cols["email"]["nullable"] is True, "email 应已放宽为可空"
    # NOT NULL 新列必须有默认值，否则 SQLite 的 ADD COLUMN 会直接失败
    assert cols["gender"]["default"] == "'unknown'", cols["gender"]
    assert cols["bio"]["default"] == "''", cols["bio"]

    ix = _indexes()
    assert ix.get("ix_users_email") is True, ix        # 唯一索引须在重建后恢复
    assert ix.get("ix_users_phone") is True, ix        # 手机号唯一性由它承载
    assert not any(name.endswith("__mig_old") for name in ix), ix
    print(f"[mig] ensure_schema ok（新增 {len(_NEW_COLUMNS)} 列 + 重建放宽 email + 补唯一索引）")


def test_data_preserved_and_fixup_applied():
    conn = sqlite3.connect(_DB_PATH)
    try:
        rows = dict(conn.execute("SELECT id, email FROM users").fetchall())
        orphan = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%__mig_old'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 2, rows                        # 数据零丢失
    assert rows["a" * 32] == "legacy@example.com"
    assert rows["b" * 32] is None, "空串 email 应归一为 NULL（否则多个纯手机号账号会互撞）"
    assert not orphan, f"临时表未清理：{orphan}"
    assert (_DB_PATH.with_name(_DB_PATH.name + ".pre-migration.bak")).is_file(), "重建前应备份"
    print("[mig] data preserved + email '' → NULL + 备份文件 ok")


def test_multiple_null_emails_coexist():
    """SQL 标准：NULL 不参与唯一性比较 → 多个「只有手机号」的账号可共存。"""
    from sqlalchemy.exc import IntegrityError

    from backend.storage.db import session
    from backend.storage.models import User

    def insert(uid: str, phone: str) -> None:
        # 其余列交给 ORM 默认值（created_at 等），只显式给标识符
        with session() as s:
            s.add(User(id=uid, email=None, phone=phone, password_hash="h", nickname="手机用户"))

    insert("c" * 32, "+8613800138000")
    try:
        insert("d" * 32, "+8613900139000")   # 同样 email=NULL
    except IntegrityError as exc:            # pragma: no cover - 失败路径
        raise AssertionError(f"两个 NULL 邮箱账号应能共存：{exc}") from exc

    # 但重复手机号必须被唯一索引拦住（唯一性静默失效是本迁移最危险的回归）
    try:
        insert("e" * 32, "+8613800138000")
    except IntegrityError:
        pass
    else:
        raise AssertionError("重复手机号应被唯一索引拒绝")
    print("[mig] NULL email 可共存 + 重复手机号被拒 ok")


def test_migration_is_idempotent():
    from backend.storage import migrations

    assert migrations.ensure_schema() == [], "二次执行不应再有任何动作"
    assert migrations.APPLIED == []
    print("[mig] idempotent ok（二次执行无动作）")


def _cleanup():
    try:
        from backend.storage import db as sdb
        sdb.engine.dispose()
    except Exception:
        pass
    try:
        import shutil
        shutil.rmtree(_TMP_DIR, ignore_errors=True)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        test_legacy_shape_before_migration()
        test_ensure_schema_aligns_structure()
        test_data_preserved_and_fixup_applied()
        test_multiple_null_emails_coexist()
        test_migration_is_idempotent()
        print("MIGRATION TEST PASSED")
    finally:
        _cleanup()
