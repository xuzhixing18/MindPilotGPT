"""share 仓储：分享行的 ORM 读写。

私有操作（创建/列表/撤销）首参 ``user_id`` 列级隔离（复刻 library/notes 范式）；
公开读按 ``code`` 点查，不带 user_id 过滤——分享本体即公开物，但响应字段白名单
由 service 层控制（user_id 永不外泄）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select

from backend.storage import models
from backend.storage.db import session


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


def _iso(value: datetime | None) -> str | None:
    """统一输出 naive UTC ISO 串（与 notes.store._iso 同因：SQLite 回读丢 tzinfo）。"""
    if not value:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat()


def _share_dict(row: models.Share) -> dict[str, Any]:
    return {
        "id": row.id,
        "code": row.code,
        "user_id": row.user_id,
        "content_key": row.content_key,
        "kind": row.kind,
        "ref_id": row.ref_id,
        "title": row.title,
        "thumb_url": row.thumb_url,
        "excerpt": row.excerpt,
        "payload": dict(row.payload or {}),
        "view_count": int(row.view_count or 0),
        "created_at": _iso(row.created_at),
        "revoked_at": _iso(row.revoked_at),
    }


# --------------------------------------------------------------------------- #
# 写
# --------------------------------------------------------------------------- #
def share_create(
    user_id: str,
    code: str,
    content_key: str,
    kind: str,
    ref_id: str,
    title: str,
    thumb_url: str,
    excerpt: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    with session() as s:
        row = models.Share(
            id=_new_id(), code=code, user_id=user_id, content_key=content_key,
            kind=kind, ref_id=ref_id, title=title, thumb_url=thumb_url,
            excerpt=excerpt, payload=payload,
        )
        s.add(row)
        s.flush()
        return _share_dict(row)


def share_update(
    share_id: str,
    title: str,
    thumb_url: str,
    excerpt: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """按 id 更新快照内容（bundle 复用同码：勾选变化链接不变）。

    只覆盖内容字段；code/user_id/content_key/kind/view_count 不变。
    """
    with session() as s:
        row = s.execute(
            select(models.Share).where(models.Share.id == share_id)
        ).scalar_one_or_none()
        if row is None:
            return None
        row.title = title
        row.thumb_url = thumb_url
        row.excerpt = excerpt
        row.payload = payload
        s.flush()
        return _share_dict(row)


def share_revoke(user_id: str, code: str) -> bool:
    """撤销（置 revoked_at）；不存在/不属于本人 → False（路由映射 404）。"""
    with session() as s:
        row = s.execute(
            select(models.Share).where(
                models.Share.user_id == user_id, models.Share.code == code,
            )
        ).scalar_one_or_none()
        if row is None or row.revoked_at is not None:
            return False
        row.revoked_at = _now()
        s.flush()
        return True


def share_bump_view(code: str) -> None:
    """浏览计数 +1（去重由 service 层 IP 窗口保证）。"""
    with session() as s:
        row = s.execute(
            select(models.Share).where(models.Share.code == code)
        ).scalar_one_or_none()
        if row is not None:
            row.view_count = int(row.view_count or 0) + 1
            s.flush()


# --------------------------------------------------------------------------- #
# 读
# --------------------------------------------------------------------------- #
def share_get(code: str) -> dict[str, Any] | None:
    """按短链码点查（公开读；含已撤销行，由调用方判 revoked_at）。"""
    with session() as s:
        row = s.execute(
            select(models.Share).where(models.Share.code == code)
        ).scalar_one_or_none()
        return _share_dict(row) if row else None


def share_find_active(
    user_id: str, content_key: str, kind: str, ref_id: str,
) -> dict[str, Any] | None:
    """幂等查询：同 (用户, 视频, 载体, 对象) 最新一条未撤销分享。"""
    with session() as s:
        row = s.execute(
            select(models.Share)
            .where(
                models.Share.user_id == user_id,
                models.Share.content_key == content_key,
                models.Share.kind == kind,
                models.Share.ref_id == ref_id,
                models.Share.revoked_at.is_(None),
            )
            .order_by(models.Share.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        return _share_dict(row) if row else None


def share_list(
    user_id: str, limit: int = 50, offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """我的分享（created_at 倒序，含已撤销供管理列表展示状态）+ 总数。"""
    with session() as s:
        stmt = select(models.Share).where(models.Share.user_id == user_id)
        total = int(s.execute(select(func.count()).select_from(stmt.subquery())).scalar() or 0)
        rows = s.execute(
            stmt.order_by(models.Share.created_at.desc()).offset(offset).limit(limit)
        ).scalars().all()
    return [_share_dict(r) for r in rows], total
