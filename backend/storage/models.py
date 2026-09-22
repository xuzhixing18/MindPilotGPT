"""ORM 模型：转写缓存与总结缓存两张表。

用 SQLAlchemy 通用 ``JSON`` 类型（SQLite/Postgres 均可用；阶段1 迁 Postgres 时
可平滑换成 JSONB 以获得索引与更强的查询能力）。表结构对上层透明，业务层只经
``repo`` 读写，不直接接触 ORM 对象。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.storage.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Transcript(Base):
    """转写结果缓存（全局共享，按规范化 URL 的哈希为主键）。"""

    __tablename__ = "transcripts"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    url: Mapped[str] = mapped_column(Text, default="")
    normalized_url: Mapped[str] = mapped_column(Text, default="", index=True)
    title: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(16), default="")  # manual / auto / asr
    language: Mapped[str] = mapped_column(String(32), default="")
    language_name: Mapped[str] = mapped_column(String(64), default="")
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    segments: Mapped[list[Any]] = mapped_column(JSON, default=list)
    text: Mapped[str] = mapped_column(Text, default="")
    asr_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    webpage_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Summary(Base):
    """AI 总结缓存（按 文本哈希 + 模型 + 提示词版本 为主键，跨 URL 复用）。"""

    __tablename__ = "summaries"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(128), default="")
    prompt_version: Mapped[str] = mapped_column(String(16), default="")
    title: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Mindmap(Base):
    """AI 思维导图缓存（按 文本哈希 + 模型 + 提示词版本 为主键，跨 URL 复用）。

    与 Summary 同构：不设 TTL，键含模型与提示词版本，任一变更即自然失效。
    """

    __tablename__ = "mindmaps"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(128), default="")
    prompt_version: Mapped[str] = mapped_column(String(16), default="")
    title: Mapped[str] = mapped_column(Text, default="")
    mindmap: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
