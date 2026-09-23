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

class Comment(Base):
    """高赞评论缓存（按规范化 URL 的哈希为主键）

    支持列注释的数据库（Postgres/MySQL，阶段1 目标）会在 DDL 中生成列注释；
    SQLite 无列注释语法，备注存于元数据，迁移后自动生效。
    """

    __tablename__ = "comments"
    __table_args__ = {"comment": "高赞评论缓存表（TTL=COMMENTS_CACHE_HOURS）"}

    # 缓存主键：comments_key(url) 的 sha256（域前缀 + 规范化 URL），同一视频唯一
    key: Mapped[str] = mapped_column(
        String(64), primary_key=True,
        comment="缓存主键：comments_key(url) 的 sha256（域前缀+规范化URL）",
    )
    # 原始视频链接（用户输入/抓取时的 URL，未规范化）
    url: Mapped[str] = mapped_column(Text, default="", comment="原始视频链接（未规范化）")
    # 规范化视频链接（去跟踪参/小写 host/排序 query），跨链接形式命中同一缓存
    normalized_url: Mapped[str] = mapped_column(
        Text, default="", index=True,
        comment="规范化视频链接（去跟踪参数，用于跨链接形式缓存命中）",
    )
    # 视频标题（展示与排查用，可为空）
    title: Mapped[str] = mapped_column(Text, default="", comment="视频标题")
    source: Mapped[str] = mapped_column(
        String(32), default="",
        comment="来源平台标识：bilibili / douyin / generic",
    )
    total: Mapped[int] = mapped_column(Integer, default=0, comment="高赞评论条数（TopN 截断后）")
    comments: Mapped[list[Any]] = mapped_column(
        JSON, default=list,
        comment="高赞评论 JSON 列表：[{author,text,likes,time}]，按点赞降序",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow,
        comment="写入/刷新时间（UTC），用于 TTL 过期判断",
    )