"""存储层门面：内容缓存（转写/总结）+ 并发去重 + SQLite/ORM 基础设施。

延续项目「门面 + 可插拔」风格：业务层（transcribe / ai.summary）只依赖本门面暴露
的少量符号，不感知底层是 SQLite 还是（阶段1 的）Postgres，也不直接接触 ORM。

阶段0：SQLite 持久化缓存 + 进程内 single-flight 去重。
阶段1：改 ``DATABASE_URL`` 迁 Postgres、single-flight 换 Redis 分布式锁，业务代码不动。
"""

from __future__ import annotations

from backend.storage import repo
from backend.storage.db import Base, init_db, session
from backend.storage.keys import comments_key, mindmap_key, normalize_url, summary_key, transcript_key
from backend.storage.singleflight import SingleFlight

# 全局单例：转写 / 总结 / 思维导图 / 评论各用一个 single-flight 通道（按 key 串行化并发相同请求）
transcribe_flight = SingleFlight()
summary_flight = SingleFlight()
mindmap_flight = SingleFlight()
comments_flight = SingleFlight()

__all__ = [
    "Base",
    "init_db",
    "session",
    "repo",
    "normalize_url",
    "transcript_key",
    "summary_key",
    "mindmap_key",
    "comments_key",
    "SingleFlight",
    "transcribe_flight",
    "summary_flight",
    "mindmap_flight",
    "comments_flight",
]
