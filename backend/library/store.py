"""library 仓储：历史 / 合集 / 问答会话的 ORM 读写。

**隔离落在签名层**：所有函数首参均为 ``user_id``（漏传即 TypeError），查询一律带
``user_id`` 过滤；命中空返回 None / False / 空列表，由 service 决定抛 NotFoundError。
业务层不直接接触 ORM 对象。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select

from backend.storage import models
from backend.storage.db import session


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _history_dict(row: models.UserHistory) -> dict[str, Any]:
    return {
        "id": row.id,
        "content_key": row.content_key,
        "url": row.url,
        "title": row.title,
        "kinds": list(row.kinds or []),
        "source": row.source,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _coll_dict(row: models.Collection) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _item_dict(row: models.CollectionItem) -> dict[str, Any]:
    return {
        "id": row.id,
        "content_key": row.content_key,
        "url": row.url,
        "title": row.title,
        "added_at": _iso(row.added_at),
    }


def _session_dict(row: models.QaSession) -> dict[str, Any]:
    return {
        "id": row.id,
        "content_key": row.content_key,
        "title": row.title,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _msg_dict(row: models.QaMessage) -> dict[str, Any]:
    return {"id": row.id, "role": row.role, "content": row.content, "created_at": _iso(row.created_at)}


# --------------------------------------------------------------------------- #
# 处理历史
# --------------------------------------------------------------------------- #
def history_upsert(
    user_id: str,
    content_key: str,
    url: str = "",
    title: str = "",
    kind: str = "",
    source: str = "",
) -> dict[str, Any]:
    """写入 / 更新历史条目：kinds 累加、title/source 空则不覆盖、刷新 updated_at。"""
    with session() as s:
        row = s.execute(
            select(models.UserHistory).where(
                models.UserHistory.user_id == user_id,
                models.UserHistory.content_key == content_key,
            )
        ).scalar_one_or_none()
        if row is None:
            row = models.UserHistory(id=_new_id(), user_id=user_id, content_key=content_key)
            s.add(row)
        if url:
            row.url = url
        if title:
            row.title = title
        if source:
            row.source = source
        kinds = list(row.kinds or [])
        if kind and kind not in kinds:
            kinds.append(kind)
            row.kinds = kinds
        row.updated_at = _now()
        s.flush()
        return _history_dict(row)


def history_list(
    user_id: str,
    q: str = "",
    kind: str = "",
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """历史列表（updated_at 倒序）+ 总数。

    kind 过滤在 Python 侧做（kinds 为 JSON 列表，跨 SQLite/Postgres 的统一contains
    查询不值得引入）；用户历史上限 500 条量级，全取再过滤成本可接受。
    """
    with session() as s:
        stmt = select(models.UserHistory).where(models.UserHistory.user_id == user_id)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(or_(models.UserHistory.title.like(like), models.UserHistory.url.like(like)))
        rows = s.execute(stmt.order_by(models.UserHistory.updated_at.desc())).scalars().all()
    items = [_history_dict(r) for r in rows]
    if kind:
        items = [it for it in items if kind in it["kinds"]]
    return items[offset:offset + limit], len(items)


def history_get(user_id: str, content_key: str) -> dict[str, Any] | None:
    with session() as s:
        row = s.execute(
            select(models.UserHistory).where(
                models.UserHistory.user_id == user_id,
                models.UserHistory.content_key == content_key,
            )
        ).scalar_one_or_none()
        return _history_dict(row) if row else None


def history_count(user_id: str) -> int:
    with session() as s:
        return int(s.execute(
            select(func.count()).select_from(models.UserHistory).where(models.UserHistory.user_id == user_id)
        ).scalar() or 0)


def history_oldest(user_id: str, n: int) -> list[str]:
    """最旧的 n 条 content_key（滚动淘汰用）。"""
    with session() as s:
        rows = s.execute(
            select(models.UserHistory.content_key)
            .where(models.UserHistory.user_id == user_id)
            .order_by(models.UserHistory.updated_at.asc())
            .limit(n)
        ).all()
    return [r[0] for r in rows]


def history_delete(user_id: str, content_key: str) -> bool:
    with session() as s:
        row = s.execute(
            select(models.UserHistory).where(
                models.UserHistory.user_id == user_id,
                models.UserHistory.content_key == content_key,
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        s.delete(row)
        return True


def history_clear(user_id: str) -> int:
    with session() as s:
        rows = s.execute(
            select(models.UserHistory).where(models.UserHistory.user_id == user_id)
        ).scalars().all()
        n = len(rows)
        for row in rows:
            s.delete(row)
        return n


def history_remove_kind(user_id: str, content_key: str, kind: str) -> None:
    """从 kinds 移除某类（如删光 QA 会话后移除 qa）。"""
    with session() as s:
        row = s.execute(
            select(models.UserHistory).where(
                models.UserHistory.user_id == user_id,
                models.UserHistory.content_key == content_key,
            )
        ).scalar_one_or_none()
        if row is None:
            return
        kinds = [k for k in (row.kinds or []) if k != kind]
        row.kinds = kinds


# --------------------------------------------------------------------------- #
# 合集
# --------------------------------------------------------------------------- #
def collection_list(user_id: str) -> list[dict[str, Any]]:
    with session() as s:
        rows = s.execute(
            select(models.Collection)
            .where(models.Collection.user_id == user_id)
            .order_by(models.Collection.updated_at.desc())
        ).scalars().all()
        ids = [r.id for r in rows]
        counts: dict[str, int] = {}
        if ids:
            counts = dict(s.execute(
                select(models.CollectionItem.collection_id, func.count())
                .where(models.CollectionItem.collection_id.in_(ids))
                .group_by(models.CollectionItem.collection_id)
            ).all())
    return [{**_coll_dict(r), "count": int(counts.get(r.id, 0))} for r in rows]


def collection_count(user_id: str) -> int:
    with session() as s:
        return int(s.execute(
            select(func.count()).select_from(models.Collection).where(models.Collection.user_id == user_id)
        ).scalar() or 0)


def collection_create(user_id: str, name: str, description: str = "") -> dict[str, Any]:
    with session() as s:
        row = models.Collection(id=_new_id(), user_id=user_id, name=name, description=description or "")
        s.add(row)
        s.flush()
        return _coll_dict(row)


def collection_get(user_id: str, coll_id: str) -> dict[str, Any] | None:
    with session() as s:
        row = s.execute(
            select(models.Collection).where(
                models.Collection.user_id == user_id, models.Collection.id == coll_id,
            )
        ).scalar_one_or_none()
        return _coll_dict(row) if row else None


def collection_update(user_id: str, coll_id: str, name: str | None, description: str | None) -> dict[str, Any] | None:
    with session() as s:
        row = s.execute(
            select(models.Collection).where(
                models.Collection.user_id == user_id, models.Collection.id == coll_id,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        if name is not None:
            row.name = name
        if description is not None:
            row.description = description
        row.updated_at = _now()
        s.flush()
        return _coll_dict(row)


def collection_delete(user_id: str, coll_id: str) -> bool:
    with session() as s:
        row = s.execute(
            select(models.Collection).where(
                models.Collection.user_id == user_id, models.Collection.id == coll_id,
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        items = s.execute(
            select(models.CollectionItem).where(models.CollectionItem.collection_id == coll_id)
        ).scalars().all()
        for item in items:
            s.delete(item)
        s.delete(row)
        return True


def item_list(user_id: str, coll_id: str, limit: int = 200) -> list[dict[str, Any]]:
    with session() as s:
        rows = s.execute(
            select(models.CollectionItem)
            .join(models.Collection, models.Collection.id == models.CollectionItem.collection_id)
            .where(models.Collection.user_id == user_id, models.CollectionItem.collection_id == coll_id)
            .order_by(models.CollectionItem.added_at.desc())
            .limit(limit)
        ).scalars().all()
    return [_item_dict(r) for r in rows]


def item_count(user_id: str, coll_id: str) -> int:
    with session() as s:
        return int(s.execute(
            select(func.count()).select_from(models.CollectionItem)
            .join(models.Collection, models.Collection.id == models.CollectionItem.collection_id)
            .where(models.Collection.user_id == user_id, models.CollectionItem.collection_id == coll_id)
        ).scalar() or 0)


def item_add(user_id: str, coll_id: str, content_key: str, url: str = "", title: str = "") -> dict[str, Any] | None:
    """入集；已存在返回 None（service 转 ValidationError）。合集不属于该用户也返回 None。"""
    with session() as s:
        owns = s.execute(
            select(models.Collection.id).where(
                models.Collection.user_id == user_id, models.Collection.id == coll_id,
            )
        ).scalar_one_or_none()
        if owns is None:
            return None
        dup = s.execute(
            select(models.CollectionItem.id).where(
                models.CollectionItem.collection_id == coll_id,
                models.CollectionItem.content_key == content_key,
            )
        ).scalar_one_or_none()
        if dup is not None:
            return None
        row = models.CollectionItem(
            id=_new_id(), collection_id=coll_id, content_key=content_key, url=url or "", title=title or "",
        )
        s.add(row)
        s.flush()
        return _item_dict(row)


def item_remove(user_id: str, coll_id: str, content_key: str) -> bool:
    with session() as s:
        row = s.execute(
            select(models.CollectionItem)
            .join(models.Collection, models.Collection.id == models.CollectionItem.collection_id)
            .where(
                models.Collection.user_id == user_id,
                models.CollectionItem.collection_id == coll_id,
                models.CollectionItem.content_key == content_key,
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        s.delete(row)
        return True


# --------------------------------------------------------------------------- #
# 问答会话
# --------------------------------------------------------------------------- #
def qa_session_create(user_id: str, content_key: str, title: str = "") -> dict[str, Any]:
    with session() as s:
        row = models.QaSession(id=_new_id(), user_id=user_id, content_key=content_key, title=title or "")
        s.add(row)
        s.flush()
        return _session_dict(row)


def qa_session_list(user_id: str, content_key: str | None = None) -> list[dict[str, Any]]:
    with session() as s:
        stmt = select(models.QaSession).where(models.QaSession.user_id == user_id)
        if content_key:
            stmt = stmt.where(models.QaSession.content_key == content_key)
        rows = s.execute(stmt.order_by(models.QaSession.updated_at.desc())).scalars().all()
    return [_session_dict(r) for r in rows]


def qa_session_count(user_id: str) -> int:
    with session() as s:
        return int(s.execute(
            select(func.count()).select_from(models.QaSession).where(models.QaSession.user_id == user_id)
        ).scalar() or 0)


def qa_session_get(user_id: str, session_id: str) -> dict[str, Any] | None:
    with session() as s:
        row = s.execute(
            select(models.QaSession).where(
                models.QaSession.user_id == user_id, models.QaSession.id == session_id,
            )
        ).scalar_one_or_none()
        return _session_dict(row) if row else None


def qa_session_rename(user_id: str, session_id: str, title: str) -> dict[str, Any] | None:
    with session() as s:
        row = s.execute(
            select(models.QaSession).where(
                models.QaSession.user_id == user_id, models.QaSession.id == session_id,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        row.title = title
        s.flush()
        return _session_dict(row)


def qa_session_delete(user_id: str, session_id: str) -> str | None:
    """删除会话与消息；返回其 content_key（供 service 同步 kinds），不存在返回 None。"""
    with session() as s:
        row = s.execute(
            select(models.QaSession).where(
                models.QaSession.user_id == user_id, models.QaSession.id == session_id,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        content_key = row.content_key
        msgs = s.execute(
            select(models.QaMessage).where(models.QaMessage.session_id == session_id)
        ).scalars().all()
        for msg in msgs:
            s.delete(msg)
        s.delete(row)
        return content_key


def qa_oldest_session_id(user_id: str) -> str | None:
    with session() as s:
        return s.execute(
            select(models.QaSession.id)
            .where(models.QaSession.user_id == user_id)
            .order_by(models.QaSession.updated_at.asc())
            .limit(1)
        ).scalar_one_or_none()


def qa_session_ids_for_content(user_id: str, content_key: str) -> list[str]:
    with session() as s:
        rows = s.execute(
            select(models.QaSession.id).where(
                models.QaSession.user_id == user_id, models.QaSession.content_key == content_key,
            )
        ).all()
    return [r[0] for r in rows]


def qa_delete_sessions_for_content(user_id: str, content_key: str) -> int:
    n = 0
    for sid in qa_session_ids_for_content(user_id, content_key):
        if qa_session_delete(user_id, sid) is not None:
            n += 1
    return n


def qa_delete_all_sessions(user_id: str) -> int:
    with session() as s:
        ids = [r[0] for r in s.execute(
            select(models.QaSession.id).where(models.QaSession.user_id == user_id)
        ).all()]
        if not ids:
            return 0
        msgs = s.execute(select(models.QaMessage).where(models.QaMessage.session_id.in_(ids))).scalars().all()
        for msg in msgs:
            s.delete(msg)
        sessions = s.execute(select(models.QaSession).where(models.QaSession.id.in_(ids))).scalars().all()
        for row in sessions:
            s.delete(row)
        return len(ids)


def qa_messages(user_id: str, session_id: str, limit: int | None = None) -> list[dict[str, Any]]:
    """会话消息（正序）；join 会话表校验归属，跨用户取不到任何消息。"""
    with session() as s:
        stmt = (
            select(models.QaMessage)
            .join(models.QaSession, models.QaSession.id == models.QaMessage.session_id)
            .where(models.QaSession.user_id == user_id, models.QaMessage.session_id == session_id)
            .order_by(models.QaMessage.created_at.asc(), models.QaMessage.id.asc())
        )
        if limit:
            stmt = stmt.limit(limit)
        rows = s.execute(stmt).scalars().all()
    return [_msg_dict(r) for r in rows]


def qa_recent(user_id: str, session_id: str, n: int) -> list[dict[str, str]]:
    """最近 n 条消息（正序返回，供 LLM 上下文组装）。"""
    with session() as s:
        rows = s.execute(
            select(models.QaMessage)
            .join(models.QaSession, models.QaSession.id == models.QaMessage.session_id)
            .where(models.QaSession.user_id == user_id, models.QaMessage.session_id == session_id)
            .order_by(models.QaMessage.created_at.desc(), models.QaMessage.id.desc())
            .limit(n)
        ).scalars().all()
    return [{"role": r.role, "content": r.content} for r in reversed(rows)]


def qa_append(user_id: str, session_id: str, role: str, content: str) -> None:
    """追加一条消息并 touch 会话 updated_at（归属不匹配时静默不写）。"""
    with session() as s:
        row = s.execute(
            select(models.QaSession).where(
                models.QaSession.user_id == user_id, models.QaSession.id == session_id,
            )
        ).scalar_one_or_none()
        if row is None:
            return
        s.add(models.QaMessage(id=_new_id(), session_id=session_id, role=role, content=content))
        row.updated_at = _now()
