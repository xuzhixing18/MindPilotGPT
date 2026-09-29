"""notes 仓储：笔记 / 图片的 ORM 读写。

**隔离落在签名层**（复刻 library/store.py 的既有范式）：所有函数首参均为
``user_id``（漏传即 TypeError），查询一律带 ``user_id`` 过滤；命中空返回
None / False / 空列表，由 service 决定抛 NotFoundError。业务层不接触 ORM 对象。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select

from backend.storage import models
from backend.storage.db import session


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


def _iso(value: datetime | None) -> str | None:
    """统一输出 naive UTC ISO 串。

    SQLite 回读的 datetime 不带 tzinfo（存时丢失），而 flush 期内的 ORM 对象带
    tzinfo：若直接 isoformat()，同一时刻会因「写入路径 vs 回读路径」输出两种格式
    （带/不带 +00:00），前端冲突基线比对与展示都不一致。统一转为 naive UTC。
    """
    if not value:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat()


def _note_dict(row: models.UserNote) -> dict[str, Any]:
    return {
        "id": row.id,
        "content_key": row.content_key,
        "url": row.url,
        "title": row.title,
        "body": row.body,
        "marks": list(row.marks or []),
        "tag": row.tag,
        "starred": bool(row.starred),
        "source_type": row.source_type,
        "source_ref": row.source_ref,
        "image_count": row.image_count,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _image_dict(row: models.NoteImage) -> dict[str, Any]:
    return {
        "id": row.id,
        "note_id": row.note_id,
        "content_key": row.content_key,
        "t": row.t,
        "url": f"/api/me/notes/images/{row.id}",
        "mime": row.mime,
        "bytes": row.bytes,
        "created_at": _iso(row.created_at),
    }


# --------------------------------------------------------------------------- #
# 笔记
# --------------------------------------------------------------------------- #
def note_create(
    user_id: str,
    content_key: str,
    body: str,
    url: str = "",
    title: str = "",
    marks: list[dict[str, Any]] | None = None,
    tag: str = "",
    source_type: str = "manual",
    source_ref: str = "",
) -> dict[str, Any]:
    with session() as s:
        row = models.UserNote(
            id=_new_id(), user_id=user_id, content_key=content_key, url=url or "",
            title=title or "", body=body, marks=marks or [], tag=tag or "",
            source_type=source_type or "manual", source_ref=source_ref or "",
        )
        s.add(row)
        s.flush()
        return _note_dict(row)


def note_get(user_id: str, note_id: str) -> dict[str, Any] | None:
    with session() as s:
        row = s.execute(
            select(models.UserNote).where(
                models.UserNote.user_id == user_id, models.UserNote.id == note_id,
            )
        ).scalar_one_or_none()
        return _note_dict(row) if row else None


def note_list(
    user_id: str,
    content_key: str = "",
    q: str = "",
    tag: str = "",
    starred: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """笔记列表（updated_at 倒序）+ 总数。content_key 留空 = 笔记库全量。"""
    with session() as s:
        stmt = select(models.UserNote).where(models.UserNote.user_id == user_id)
        if content_key:
            stmt = stmt.where(models.UserNote.content_key == content_key)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(models.UserNote.body.like(like))
        if tag:
            stmt = stmt.where(models.UserNote.tag == tag)
        if starred:
            stmt = stmt.where(models.UserNote.starred.is_(True))
        total = int(s.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar() or 0)
        rows = s.execute(
            stmt.order_by(models.UserNote.updated_at.desc()).offset(offset).limit(limit)
        ).scalars().all()
    return [_note_dict(r) for r in rows], total


def note_update(
    user_id: str,
    note_id: str,
    body: str | None = None,
    title: str | None = None,
    tag: str | None = None,
    starred: bool | None = None,
    marks: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """更新笔记；返回更新后版本，不存在/不属于本人返回 None。updated_at 由 onupdate 刷新。"""
    with session() as s:
        row = s.execute(
            select(models.UserNote).where(
                models.UserNote.user_id == user_id, models.UserNote.id == note_id,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        if body is not None:
            row.body = body
        if title is not None:
            row.title = title
        if tag is not None:
            row.tag = tag
        if starred is not None:
            row.starred = starred
        if marks is not None:
            row.marks = marks
        row.updated_at = _now()
        s.flush()
        return _note_dict(row)


def note_delete(user_id: str, note_id: str) -> str | None:
    """删除笔记行；返回其 content_key（供 service 同步历史 kinds），不存在返回 None。"""
    with session() as s:
        row = s.execute(
            select(models.UserNote).where(
                models.UserNote.user_id == user_id, models.UserNote.id == note_id,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        content_key = row.content_key
        s.delete(row)
        return content_key


def note_count(user_id: str) -> int:
    with session() as s:
        return int(s.execute(
            select(func.count()).select_from(models.UserNote).where(models.UserNote.user_id == user_id)
        ).scalar() or 0)


def note_oldest_ids(user_id: str, n: int) -> list[str]:
    """最旧不活跃的 n 条笔记 id（滚动淘汰用）。"""
    with session() as s:
        rows = s.execute(
            select(models.UserNote.id)
            .where(models.UserNote.user_id == user_id)
            .order_by(models.UserNote.updated_at.asc())
            .limit(n)
        ).all()
    return [r[0] for r in rows]


def note_ids_for_content(user_id: str, content_key: str) -> list[str]:
    with session() as s:
        rows = s.execute(
            select(models.UserNote.id).where(
                models.UserNote.user_id == user_id,
                models.UserNote.content_key == content_key,
            )
        ).all()
    return [r[0] for r in rows]


# --------------------------------------------------------------------------- #
# 图片
# --------------------------------------------------------------------------- #
def image_create(
    user_id: str,
    image_id: str,
    content_key: str,
    mime: str,
    nbytes: int,
    t: float | None = None,
    note_id: str | None = None,
) -> dict[str, Any]:
    with session() as s:
        row = models.NoteImage(
            id=image_id, user_id=user_id, note_id=note_id, content_key=content_key or "",
            t=t, mime=mime, bytes=nbytes,
        )
        s.add(row)
        s.flush()
        return _image_dict(row)


def image_get(user_id: str, image_id: str) -> dict[str, Any] | None:
    with session() as s:
        row = s.execute(
            select(models.NoteImage).where(
                models.NoteImage.user_id == user_id, models.NoteImage.id == image_id,
            )
        ).scalar_one_or_none()
        return _image_dict(row) if row else None


def image_delete(user_id: str, image_id: str) -> str | None:
    """删图片行；返回其 note_id（可能为 None：孤儿），不存在返回 None。"""
    with session() as s:
        row = s.execute(
            select(models.NoteImage).where(
                models.NoteImage.user_id == user_id, models.NoteImage.id == image_id,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        note_id = row.note_id
        s.delete(row)
        return note_id


def images_for_note(user_id: str, note_id: str) -> list[dict[str, Any]]:
    with session() as s:
        rows = s.execute(
            select(models.NoteImage).where(
                models.NoteImage.user_id == user_id, models.NoteImage.note_id == note_id,
            ).order_by(models.NoteImage.created_at.asc())
        ).scalars().all()
    return [_image_dict(r) for r in rows]


def image_claim_for_content(user_id: str, note_id: str, content_key: str) -> int:
    """把该视频下未被认领的孤儿图片归属到笔记（新建未保存时贴图的认领）。返回认领数。"""
    with session() as s:
        rows = s.execute(
            select(models.NoteImage).where(
                models.NoteImage.user_id == user_id,
                models.NoteImage.content_key == content_key,
                models.NoteImage.note_id.is_(None),
            )
        ).scalars().all()
        for row in rows:
            row.note_id = note_id
        return len(rows)


def image_count_for_note(user_id: str, note_id: str) -> int:
    with session() as s:
        return int(s.execute(
            select(func.count()).select_from(models.NoteImage).where(
                models.NoteImage.user_id == user_id, models.NoteImage.note_id == note_id,
            )
        ).scalar() or 0)


def image_total_bytes(user_id: str) -> int:
    with session() as s:
        return int(s.execute(
            select(func.coalesce(func.sum(models.NoteImage.bytes), 0)).where(
                models.NoteImage.user_id == user_id,
            )
        ).scalar() or 0)


def image_orphans(
    user_id: str,
    max_age_seconds: int,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """超龄孤儿图片（note_id 为空）：惰性清扫候选。"""
    from datetime import timedelta

    cutoff = _now() - timedelta(seconds=max_age_seconds)
    with session() as s:
        rows = s.execute(
            select(models.NoteImage).where(
                models.NoteImage.user_id == user_id,
                models.NoteImage.note_id.is_(None),
                models.NoteImage.created_at < cutoff,
            ).order_by(models.NoteImage.created_at.asc()).limit(limit)
        ).scalars().all()
    return [_image_dict(r) for r in rows]


def image_delete_rows_by_note(note_id: str) -> list[str]:
    """按 note_id 删全部图片行（note 已在事务外删除，纯行级清理），返回被删图片 id。"""
    with session() as s:
        rows = s.execute(
            select(models.NoteImage).where(models.NoteImage.note_id == note_id)
        ).scalars().all()
        ids = [r.id for r in rows]
        if ids:
            s.execute(delete(models.NoteImage).where(models.NoteImage.note_id == note_id))
    return ids
