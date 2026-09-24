"""缓存仓储：转写 / 总结结果的读写（对上层屏蔽 ORM 细节）。

- 读：转写按 ``TRANSCRIPT_CACHE_DAYS``（默认 30 天）判断过期，过期视为未命中；
  总结不设 TTL（其键含模型与提示词版本，变更即自然失效）。
- 写：upsert（存在则更新），并刷新 ``created_at``。
- 开关：``CACHE_ENABLED=false`` 时读一律未命中、写直接跳过（便于对照与排障）。

存储的是「纯数据」，不含运行时的 ``cached`` 标记——命中与否由服务层负责标注。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.storage import models
from backend.storage.db import session

_FALSEY = {"0", "false", "no", "off", ""}


def _enabled() -> bool:
    """缓存总开关（默认开启）。"""
    return (os.getenv("CACHE_ENABLED") or "true").strip().lower() not in _FALSEY


def _ttl_days() -> int:
    try:
        return int((os.getenv("TRANSCRIPT_CACHE_DAYS") or "30").strip())
    except ValueError:
        return 30


def _expired(created_at: datetime | None) -> bool:
    """转写是否已过 TTL；``TRANSCRIPT_CACHE_DAYS<=0`` 视为永不过期。"""
    if created_at is None:
        return True
    days = _ttl_days()
    if days <= 0:
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created_at > timedelta(days=days)


def _transcript_to_dict(row: models.Transcript) -> dict[str, Any]:
    """ORM 行 → 与 subtitles/asr 转写结果同构的 dict（不含 cached 标记）。"""
    return {
        "title": row.title,
        "language": row.language,
        "language_name": row.language_name,
        "source": row.source,
        "segments": row.segments or [],
        "text": row.text,
        "char_count": row.char_count,
        "webpage_url": row.webpage_url or row.url,
        "asr_provider": row.asr_provider,
    }


def get_transcript(key: str) -> dict[str, Any] | None:
    """按 key 取转写缓存；未命中 / 已过期 / 缓存关闭 → None。"""
    if not _enabled():
        return None
    with session() as s:
        row = s.get(models.Transcript, key)
        if row is None or _expired(row.created_at):
            return None
        return _transcript_to_dict(row)


def put_transcript(key: str, result: dict[str, Any], url: str, normalized_url: str = "") -> None:
    """写入 / 更新转写缓存（upsert）。"""
    if not _enabled():
        return
    with session() as s:
        row = s.get(models.Transcript, key)
        if row is None:
            row = models.Transcript(key=key)
            s.add(row)
        row.url = url or ""
        row.normalized_url = normalized_url or ""
        row.title = result.get("title") or ""
        row.source = result.get("source") or ""
        row.language = result.get("language") or ""
        row.language_name = result.get("language_name") or ""
        row.char_count = int(result.get("char_count") or 0)
        row.segments = result.get("segments") or []
        row.text = result.get("text") or ""
        row.asr_provider = result.get("asr_provider")
        row.webpage_url = result.get("webpage_url")
        row.created_at = datetime.now(timezone.utc)


def get_summary(key: str) -> dict[str, Any] | None:
    """按 key 取总结缓存；未命中 / 缓存关闭 → None（返回纯 summary 数据）。"""
    if not _enabled():
        return None
    with session() as s:
        row = s.get(models.Summary, key)
        if row is None:
            return None
        return dict(row.summary or {})


def put_summary(
    key: str,
    summary: dict[str, Any],
    *,
    model: str,
    prompt_version: str,
    title: str = "",
) -> None:
    """写入 / 更新总结缓存（upsert）；剔除运行时 cached 标记后再存。"""
    if not _enabled():
        return
    clean = {k: v for k, v in (summary or {}).items() if k != "cached"}
    with session() as s:
        row = s.get(models.Summary, key)
        if row is None:
            row = models.Summary(key=key)
            s.add(row)
        row.model = model or ""
        row.prompt_version = prompt_version or ""
        row.title = title or ""
        row.summary = clean
        row.created_at = datetime.now(timezone.utc)


def get_mindmap(key: str) -> dict[str, Any] | None:
    """按 key 取思维导图缓存；未命中 / 缓存关闭 → None（返回纯 mindmap 数据）。"""
    if not _enabled():
        return None
    with session() as s:
        row = s.get(models.Mindmap, key)
        if row is None:
            return None
        return dict(row.mindmap or {})


def put_mindmap(
    key: str,
    mindmap: dict[str, Any],
    *,
    model: str,
    prompt_version: str,
    title: str = "",
) -> None:
    """写入 / 更新思维导图缓存（upsert）；剔除运行时 cached 标记后再存。"""
    if not _enabled():
        return
    clean = {k: v for k, v in (mindmap or {}).items() if k != "cached"}
    with session() as s:
        row = s.get(models.Mindmap, key)
        if row is None:
            row = models.Mindmap(key=key)
            s.add(row)
        row.model = model or ""
        row.prompt_version = prompt_version or ""
        row.title = title or ""
        row.mindmap = clean
        row.created_at = datetime.now(timezone.utc)


def _comments_ttl_hours() -> int:
    try:
        return int((os.getenv("COMMENTS_CACHE_HOURS") or "12").strip())
    except ValueError:
        return 12


def _comments_expired(created_at: datetime | None) -> bool:
    """评论是否已过 TTL；``COMMENTS_CACHE_HOURS<=0`` 视为永不过期。"""
    if created_at is None:
        return True
    hours = _comments_ttl_hours()
    if hours <= 0:
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created_at > timedelta(hours=hours)


def _comment_to_dict(row: models.Comment) -> dict[str, Any]:
    """ORM 行 → 与门面返回同构的 dict（不含 cached 标记）。"""
    return {
        "title": row.title,
        "source": row.source,
        "total": row.total,
        "comments": row.comments or [],
    }


def get_comments(key: str) -> dict[str, Any] | None:
    """按 key 取评论缓存；未命中 / 已过期 / 缓存关闭 → None。"""
    if not _enabled():
        return None
    with session() as s:
        row = s.get(models.Comment, key)
        if row is None or _comments_expired(row.created_at):
            return None
        return _comment_to_dict(row)


def put_comments(key: str, result: dict[str, Any], url: str, normalized_url: str = "") -> None:
    """写入 / 更新评论缓存（upsert）。"""
    if not _enabled():
        return
    with session() as s:
        row = s.get(models.Comment, key)
        if row is None:
            row = models.Comment(key=key)
            s.add(row)
        row.url = url or ""
        row.normalized_url = normalized_url or ""
        row.title = result.get("title") or ""
        row.source = result.get("source") or ""
        row.total = int(result.get("total") or 0)
        row.comments = result.get("comments") or []
        row.created_at = datetime.now(timezone.utc)


def _info_ttl_hours() -> int:
    try:
        return int((os.getenv("INFO_CACHE_HOURS") or "24").strip())
    except ValueError:
        return 24


def _info_expired(created_at: datetime | None) -> bool:
    """视频信息是否已过 TTL；``INFO_CACHE_HOURS<=0`` 视为永不过期。"""
    if created_at is None:
        return True
    hours = _info_ttl_hours()
    if hours <= 0:
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created_at > timedelta(hours=hours)


def get_info(key: str) -> dict[str, Any] | None:
    """按 key 取视频信息缓存；未命中 / 已过期 / 缓存关闭 → None。"""
    if not _enabled():
        return None
    with session() as s:
        row = s.get(models.VideoInfo, key)
        if row is None or _info_expired(row.created_at):
            return None
        return dict(row.payload or {})


def put_info(key: str, payload: dict[str, Any], url: str, normalized_url: str = "") -> None:
    """写入 / 更新视频信息缓存（upsert）；剔除运行时 cached 标记后再存。"""
    if not _enabled():
        return
    clean = {k: v for k, v in (payload or {}).items() if k != "cached"}
    with session() as s:
        row = s.get(models.VideoInfo, key)
        if row is None:
            row = models.VideoInfo(key=key)
            s.add(row)
        row.url = url or ""
        row.normalized_url = normalized_url or ""
        row.payload = clean
        row.created_at = datetime.now(timezone.utc)
