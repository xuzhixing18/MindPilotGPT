"""存储层数据库基础设施：engine / Session / Base / init_db。

阶段0 用 SQLite（单文件、零运维），经 SQLAlchemy ORM 抽象；阶段1 只需把
``DATABASE_URL`` 换成 ``postgresql+psycopg://...``，业务代码零改动即可迁移。

设计要点：
- engine 在导入时按 ``DATABASE_URL`` 环境变量创建（默认 ``data/mindpilot.db``）；
- sqlite 开启 WAL + check_same_thread=False，兼顾并发读与 FastAPI 线程池；
- ``init_db()`` 幂等：建目录、注册表、create_all、**结构对齐**（见 storage.migrations），
  返回是否成功（供 /api/health 展示）。
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

log = logging.getLogger(__name__)

# 项目根目录（backend/storage/db.py → storage → backend → root）
_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DB_PATH = _ROOT / "data" / "mindpilot.db"

# 尽力加载 .env（override=False：不覆盖已存在的进程/系统环境变量，测试仍可预设 DATABASE_URL 隔离）。
# 必须在此加载：db.py 可能先于 ai.config 被导入，若不加载则 .env 里的 DATABASE_URL 不生效、
# 静默回退 SQLite，造成「改了 .env 却仍连 SQLite」的配置陷阱。
try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")
except ImportError:  # pragma: no cover - python-dotenv 可选
    pass


def _database_url() -> str:
    """读取数据库 URL；未配置时回退到项目内 SQLite 文件。"""
    url = (os.getenv("DATABASE_URL") or "").strip()
    if url:
        return url
    return f"sqlite:///{_DEFAULT_DB_PATH.as_posix()}"


class Base(DeclarativeBase):
    """所有 ORM 模型的声明基类。"""


DATABASE_URL = _database_url()
_IS_SQLITE = DATABASE_URL.startswith("sqlite")

# sqlite 需 check_same_thread=False 以适配 FastAPI 线程池；其他驱动不需要该参数
_connect_args = {"check_same_thread": False} if _IS_SQLITE else {}

# Postgres 需连接池调优：pre_ping 防服务端重启/网络抖动后的 stale 连接，recycle 防云 LB/
# 防火墙掐断空闲连接；SQLite 单文件无此问题，保持默认池行为。
_pool_kwargs = (
    {}
    if _IS_SQLITE
    else {
        "pool_size": 10,
        "max_overflow": 20,
        "pool_pre_ping": True,
        "pool_recycle": 1800,
    }
)

engine = create_engine(DATABASE_URL, connect_args=_connect_args, future=True, **_pool_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)

if _IS_SQLITE:

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _record):  # pragma: no cover - 驱动回调
        """每个新连接开启 WAL（并发读）与 NORMAL 同步（性能/安全折中）。"""
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
        finally:
            cur.close()


@contextmanager
def session() -> Iterator[Session]:
    """事务性会话上下文：正常提交、异常回滚、始终关闭。"""
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def init_db() -> bool:
    """创建数据目录与所有表，并对齐既有表结构（幂等）。

    成功返回 True，失败返回 False（不抛出，避免拖垮启动）。
    ``create_all`` 只建缺失的表，因此额外调 ``migrations.ensure_schema()`` 给已存在的表
    补列 / 放宽约束 / 补索引；该步失败只记日志，不改变本函数返回值（表已可用）。
    """
    try:
        # 文件型 sqlite：确保父目录存在
        if _IS_SQLITE and ":memory:" not in DATABASE_URL:
            db_path = Path(DATABASE_URL.split("sqlite:///", 1)[-1])
            if str(db_path) != DATABASE_URL:  # 解析成功
                db_path.parent.mkdir(parents=True, exist_ok=True)
        # 延迟导入 models，确保表已注册到 Base.metadata（避免与 db 的循环导入）
        from backend.storage import models  # noqa: F401

        Base.metadata.create_all(engine)
    except Exception:
        return False

    try:
        from backend.storage import migrations

        applied = migrations.ensure_schema()
        if applied:
            log.info("[migrations] 已对齐表结构：%s", "; ".join(applied))
    except Exception as exc:
        log.error("[migrations] 结构对齐失败（旧库可能缺列）：%s", exc)
    return True
