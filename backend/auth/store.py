"""认证数据访问：用户与会话的读写（对上层屏蔽 ORM 细节，返回纯 dict）。

延续 storage.repo 的风格：业务层（service / dependencies）只依赖本模块的少量函数，
不直接接触 ORM 对象。所有时间以 UTC 存取；SQLite 读回的 naive 时间统一按 UTC 解释
（与 repo._expired 的处理一致）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from backend.auth import security
from backend.storage import models
from backend.storage.db import session


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime | None) -> datetime | None:
    """把可能是 naive 的时间统一成 UTC-aware（SQLite 读回不带 tzinfo）。"""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _user_to_dict(row: models.User) -> dict[str, Any]:
    """ORM 用户行 → 纯 dict（含 password_hash，仅供内部校验，勿直接回传前端）。"""
    return {
        "id": row.id,
        "email": row.email,
        "phone": row.phone,
        "nickname": row.nickname,
        "avatar_url": row.avatar_url,
        "bio": row.bio,
        "gender": row.gender,
        "birthday": row.birthday,
        "location": row.location,
        "website": row.website,
        "status": row.status,
        "email_verified": bool(row.email_verified),
        "phone_verified": bool(row.phone_verified),
        "plan_id": row.plan_id,
        "org_id": row.org_id,
        "privacy_mode": bool(row.privacy_mode),
        "password_hash": row.password_hash,
        "failed_login_count": int(row.failed_login_count or 0),
        "locked_until": _as_utc(row.locked_until),
        "created_at": _as_utc(row.created_at),
        "updated_at": _as_utc(row.updated_at),
    }


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    """剔除敏感字段（password_hash / 锁定计数 / 状态）后的可回传个人资料。

    同时充当登录响应与 ``/me`` 的载荷，字段一次到位，前端无需二次请求。
    """
    def iso(key: str) -> str | None:
        value = user.get(key)
        return value.isoformat() if value else None

    return {
        "id": user.get("id"),
        "email": user.get("email") or None,
        "phone": user.get("phone") or None,
        "nickname": user.get("nickname") or "",
        "avatar_url": user.get("avatar_url") or None,
        "bio": user.get("bio") or "",
        "gender": user.get("gender") or "unknown",
        "birthday": iso("birthday"),
        "location": user.get("location") or "",
        "website": user.get("website") or None,
        "plan_id": user.get("plan_id") or "free",
        "privacy_mode": bool(user.get("privacy_mode")),
        "email_verified": bool(user.get("email_verified")),
        "phone_verified": bool(user.get("phone_verified")),
        "created_at": iso("created_at"),
        "updated_at": iso("updated_at"),
    }


# --------------------------------------------------------------------------- #
# 用户
# --------------------------------------------------------------------------- #
def find_user_by_email(email: str | None) -> dict[str, Any] | None:
    """按邮箱（小写）查用户；未找到返回 None。"""
    value = (email or "").strip().lower()
    if not value:
        return None
    with session() as s:
        row = s.query(models.User).filter(models.User.email == value).one_or_none()
        return _user_to_dict(row) if row else None


def find_user_by_phone(phone: str | None) -> dict[str, Any] | None:
    """按手机号（E.164）查用户；未找到返回 None。入参需已经 identifiers 规范化。"""
    value = (phone or "").strip()
    if not value:
        return None
    with session() as s:
        row = s.query(models.User).filter(models.User.phone == value).one_or_none()
        return _user_to_dict(row) if row else None


def identifier_taken(
    *, email: str | None = None, phone: str | None = None, exclude_user_id: str | None = None,
) -> str | None:
    """返回已被占用的标识符类型（``"email"`` / ``"phone"``）；均空闲 → None。

    ``exclude_user_id`` 用于换绑场景：排除自己，避免「改回当前值」被误判为重复。
    """
    email_value = (email or "").strip().lower()
    phone_value = (phone or "").strip()
    if not email_value and not phone_value:
        return None
    with session() as s:
        q = s.query(models.User.id)
        if exclude_user_id:
            q = q.filter(models.User.id != exclude_user_id)
        if email_value and q.filter(models.User.email == email_value).first():
            return "email"
        if phone_value and q.filter(models.User.phone == phone_value).first():
            return "phone"
    return None


def find_user_by_id(user_id: str) -> dict[str, Any] | None:
    """按主键查用户；未找到返回 None。"""
    if not user_id:
        return None
    with session() as s:
        row = s.get(models.User, user_id)
        return _user_to_dict(row) if row else None


def insert_user(
    *, email: str | None = None, phone: str | None = None,
    password_hash: str, nickname: str = "",
) -> dict[str, Any]:
    """新建用户并落库，返回用户 dict（含 id）。调用方需先确保标识符唯一。"""
    uid = security.new_id()
    email_value = (email or "").strip().lower() or None
    phone_value = (phone or "").strip() or None
    # 昵称缺省：邮箱前缀 > 「用户+手机尾号」 > 「用户+短 ID」
    fallback = (
        (email_value.split("@", 1)[0] if email_value else "")
        or (f"用户{phone_value[-4:]}" if phone_value else "")
        or f"用户{uid[:6]}"
    )
    with session() as s:
        s.add(models.User(
            id=uid,
            email=email_value,
            phone=phone_value,
            password_hash=password_hash,
            nickname=nickname or fallback,
            status="active",
        ))
    return find_user_by_id(uid) or {"id": uid, "email": email_value, "nickname": nickname}


def update_user_fields(user_id: str, **fields: Any) -> dict[str, Any] | None:
    """按列名批量更新用户字段，返回更新后的用户 dict；用户不存在 → None。

    白名单校验：传入未知列名直接报错（编程错误要早暴露，而不是静默丢弃）；
    字段值的**业务校验由 service 层负责**，本函数只管落库。
    """
    columns = {c.name for c in models.User.__table__.columns}
    unknown = sorted(set(fields) - columns)
    if unknown:
        raise ValueError(f"users 表无此列：{unknown}")
    if not fields:
        return find_user_by_id(user_id)
    with session() as s:
        row = s.get(models.User, user_id)
        if row is None:
            return None
        for key, value in fields.items():
            setattr(row, key, value)
    return find_user_by_id(user_id)


def register_login_failure(
    user_id: str, *, max_attempts: int, lockout_minutes: int
) -> tuple[int, datetime | None]:
    """累加连续失败次数；达阈值则设置锁定到期时间。返回 (failed_count, locked_until)。"""
    now = _utcnow()
    with session() as s:
        row = s.get(models.User, user_id)
        if row is None:
            return 0, None
        count = int(row.failed_login_count or 0) + 1
        row.failed_login_count = count
        locked_until: datetime | None = None
        if max_attempts > 0 and count >= max_attempts:
            locked_until = now + timedelta(minutes=max(lockout_minutes, 0))
            row.locked_until = locked_until
            row.status = "locked"
        return count, locked_until


def reset_login_failure(user_id: str) -> None:
    """登录成功后清零失败计数并解锁（status 由 locked 恢复为 active）。"""
    with session() as s:
        row = s.get(models.User, user_id)
        if row is None:
            return
        row.failed_login_count = 0
        row.locked_until = None
        if row.status == "locked":
            row.status = "active"


def update_password_hash(user_id: str, password_hash: str) -> None:
    """更新密码哈希（改密 / 哈希升级时用）。"""
    with session() as s:
        row = s.get(models.User, user_id)
        if row is not None:
            row.password_hash = password_hash


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #
def create_session(
    *, token_hash: str, user_id: str, ttl_hours: int, user_agent: str = "", ip: str = ""
) -> datetime:
    """创建会话，返回过期时间（UTC）。"""
    now = _utcnow()
    expires_at = now + timedelta(hours=max(int(ttl_hours), 1))
    with session() as s:
        s.add(models.UserSession(
            id=token_hash,
            user_id=user_id,
            expires_at=expires_at,
            user_agent=(user_agent or "")[:512],
            ip=(ip or "")[:64],
            created_at=now,
        ))
    return expires_at


def load_valid_session(token_hash: str) -> dict[str, Any] | None:
    """按令牌哈希取「有效」会话对应的用户；过期/撤销/用户不存在或禁用 → None。"""
    if not token_hash:
        return None
    now = _utcnow()
    with session() as s:
        sess = s.get(models.UserSession, token_hash)
        if sess is None or sess.revoked_at is not None:
            return None
        if _as_utc(sess.expires_at) is not None and _as_utc(sess.expires_at) <= now:
            return None
        user = s.get(models.User, sess.user_id)
        if user is None or user.status == "disabled":
            return None
        return _user_to_dict(user)


def revoke_session(token_hash: str) -> None:
    """撤销单个会话（登出）。"""
    if not token_hash:
        return
    with session() as s:
        sess = s.get(models.UserSession, token_hash)
        if sess is not None and sess.revoked_at is None:
            sess.revoked_at = _utcnow()


def revoke_all_sessions(user_id: str) -> None:
    """撤销某用户全部会话（安全事件时「全量踢下线」）。"""
    now = _utcnow()
    with session() as s:
        rows = s.query(models.UserSession).filter(
            models.UserSession.user_id == user_id,
            models.UserSession.revoked_at.is_(None),
        ).all()
        for sess in rows:
            sess.revoked_at = now


def revoke_other_sessions(user_id: str, *, keep_token: str = "") -> int:
    """撤销除 ``keep_token``（明文）之外的全部有效会话，返回撤销条数。

    用于改密/换绑后「踢掉其他设备但不断自己」；``keep_token`` 为空时等价于全量撤销。
    """
    keep_hash = security.hash_token(keep_token) if keep_token else ""
    now = _utcnow()
    with session() as s:
        rows = s.query(models.UserSession).filter(
            models.UserSession.user_id == user_id,
            models.UserSession.revoked_at.is_(None),
            models.UserSession.id != keep_hash,
        ).all()
        for sess in rows:
            sess.revoked_at = now
        return len(rows)


def purge_expired_sessions() -> int:
    """物理删除已过期或已撤销超过 1 天的会话（定时清理，防表膨胀）。返回删除条数。"""
    cutoff = _utcnow() - timedelta(days=1)
    with session() as s:
        rows = s.query(models.UserSession).filter(
            (models.UserSession.expires_at <= _utcnow())
            | (models.UserSession.revoked_at.is_not(None) & (models.UserSession.revoked_at <= cutoff))
        ).all()
        count = len(rows)
        for sess in rows:
            s.delete(sess)
        return count
