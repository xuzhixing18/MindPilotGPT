"""认证与个人资料业务逻辑（编排 store + security + identifiers + avatar + config）。

对外只依赖本模块的门面函数，不感知 ORM、哈希与文件落盘细节。异常统一为 errors.py
的语义化类型，由 router 映射为合适的 HTTP 状态码。

安全要点：
- 登录失败对「用户不存在」与「密码错误」返回同一异常，防账号枚举；
- 连续失败达阈值临时锁定账号（login_max_attempts / login_lockout_minutes）；
- 成功登录后按需升级老旧密码哈希（needs_rehash）；
- 改密 / 换绑邮箱手机号均需**当前密码**确认，且失败同样计入锁定计数
  （否则这条路径会成为绕过锁定的密码爆破口子）；
- 改密后踢掉其他设备会话，不断当前会话；
- 个人网站只收 http/https，挡住 ``javascript:`` / ``data:`` 等可执行协议（前端会渲染成链接）。

手机号当前仅做**格式**校验（未接短信服务商），故 ``phone_verified`` 恒为 False，
前端如实展示「未验证」；接入验证码轮次时只需在 register / change_phone 里改该字段。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any

from backend.auth import avatar, identifiers, security, store
from backend.auth.config import load_settings
from backend.ai import config as ai_config
from backend.auth.errors import (
    AccountDisabledError,
    AccountLockedError,
    InvalidCredentialsError,
    NotAuthenticatedError,
    UserExistsError,
    ValidationError,
)

# 性别枚举（存键，本地化文案由前端负责）
GENDERS = ("unknown", "male", "female", "other")

# 已含协议头的写法（用于判断是否需补 https://）
_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):")
_WEBSITE_RE = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.IGNORECASE)


def _validate_password(password: str) -> str:
    settings = load_settings()
    if not password or len(password) < settings.password_min_length:
        raise ValidationError(f"密码至少 {settings.password_min_length} 位。")
    if len(password) > 256:
        raise ValidationError("密码过长（最多 256 位）。")
    return password


def _validate_nickname(raw: Any, *, allow_empty: bool = False) -> str:
    """昵称：折叠连续空白 + 限长；注册时可空（由 store 生成缺省昵称）。"""
    settings = load_settings()
    text = re.sub(r"\s+", " ", str(raw or "")).strip()
    if not text:
        if allow_empty:
            return ""
        raise ValidationError("昵称不能为空。")
    if len(text) > settings.nickname_max_length:
        raise ValidationError(f"昵称最多 {settings.nickname_max_length} 个字符。")
    return text


def _validate_bio(raw: Any) -> str:
    """个性签名：限长（超长直接报错而不静默截断，避免用户以为已保存全文）。"""
    settings = load_settings()
    text = str(raw or "").strip()
    if len(text) > settings.bio_max_length:
        raise ValidationError(f"个性签名最多 {settings.bio_max_length} 个字符。")
    return text


def _validate_gender(raw: Any) -> str:
    value = str(raw or "unknown").strip().lower() or "unknown"
    if value not in GENDERS:
        raise ValidationError("性别取值不正确。")
    return value


def _validate_birthday(raw: Any) -> date | None:
    """生日：接受 ``YYYY-MM-DD`` / ``YYYY-MM``（日补 1）；空 → None（清除）。"""
    if raw is None or str(raw).strip() == "":
        return None
    text = str(raw).strip()
    value: date | None = None
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y/%m/%d"):
        try:
            value = datetime.strptime(text, fmt).date()
            break
        except ValueError:
            continue
    if value is None:
        raise ValidationError("生日格式不正确，请用 YYYY-MM-DD。")
    if value > date.today():
        raise ValidationError("生日不能晚于今天。")
    if value.year < 1900:
        raise ValidationError("生日年份不正确。")
    return value


def _validate_location(raw: Any) -> str:
    text = re.sub(r"\s+", " ", str(raw or "")).strip()
    if len(text) > 64:
        raise ValidationError("所在地区最多 64 个字符。")
    return text


def _validate_website(raw: Any) -> str | None:
    """个人网站：补全 scheme，仅允许 http/https；空 → None（清除）。"""
    if raw is None or str(raw).strip() == "":
        return None
    text = str(raw).strip()
    scheme = _SCHEME_RE.match(text)
    if scheme:
        if scheme.group(1).lower() not in ("http", "https"):
            raise ValidationError("个人网站仅支持 http/https 链接。")
    else:
        text = "https://" + text.lstrip("/")  # 用户常省略协议
    if len(text) > 255 or not _WEBSITE_RE.match(text):
        raise ValidationError("个人网站格式不正确，请填写有效链接。")
    return text


def register(
    email: str | None = None,
    password: str = "",
    nickname: str = "",
    *,
    phone: str | None = None,
) -> dict[str, Any]:
    """注册新用户（邮箱与手机号**至少其一**），返回可回传的个人资料。

    两个标识符都可同时提供；入库前统一规范化（邮箱转小写、手机号转 E.164），
    以保证唯一索引能拦住同一号码/邮箱的不同写法。

    :raises ValidationError: 两个标识符都未填、格式错误或密码过弱
    :raises UserExistsError: 邮箱或手机号已被注册
    """
    settings = load_settings()
    email_value = identifiers.validate_email(email) if (email or "").strip() else None
    phone_value = (
        identifiers.validate_phone(phone, region=settings.phone_default_region)
        if (phone or "").strip() else None
    )
    if not email_value and not phone_value:
        raise ValidationError("请至少填写邮箱或手机号其中一项。")
    password = _validate_password(password)

    taken = store.identifier_taken(email=email_value, phone=phone_value)
    if taken == identifiers.EMAIL:
        raise UserExistsError("该邮箱已注册，请直接登录。")
    if taken == identifiers.PHONE:
        raise UserExistsError("该手机号已注册，请直接登录。")

    user = store.insert_user(
        email=email_value,
        phone=phone_value,
        password_hash=security.hash_password(password),
        nickname=_validate_nickname(nickname, allow_empty=True),
    )
    return store.public_user(user)


def _ensure_not_locked_or_disabled(user: dict[str, Any]) -> None:
    if user.get("status") == "disabled":
        raise AccountDisabledError("账号已被禁用。")
    locked_until = user.get("locked_until")
    if locked_until is not None:
        now = datetime.now(timezone.utc)
        if locked_until > now:
            retry_after = int((locked_until - now).total_seconds() // 60) + 1
            raise AccountLockedError(f"账号因多次登录失败被临时锁定，请约 {retry_after} 分钟后再试。")


def login(
    identifier: str,
    password: str,
    *,
    user_agent: str = "",
    ip: str = "",
    remember: bool = False,
) -> tuple[dict[str, Any], str, datetime]:
    """按「邮箱或手机号」校验凭证并创建会话。

    :param identifier: 登录框的单个输入；含 ``@`` 视为邮箱，否则按手机号处理
    :param remember: 勾选「记住我」时用更长的会话有效期（remember_ttl_hours）
    :return: (可回传个人资料, 会话令牌明文, 过期时间)
    :raises ValidationError: 标识符格式错误
    :raises InvalidCredentialsError: 账号或密码错误（不区分二者，防枚举）
    :raises AccountLockedError: 账号被锁定
    :raises AccountDisabledError: 账号被禁用
    """
    settings = load_settings()
    kind, value = identifiers.parse_identifier(identifier, region=settings.phone_default_region)
    user = (
        store.find_user_by_email(value)
        if kind == identifiers.EMAIL
        else store.find_user_by_phone(value)
    )

    # 账号不存在：仍执行一次「假校验」以拉平时延，弱化时序侧信道，然后统一报凭证错误
    if user is None:
        security.verify_password(password or "", security.hash_password("__noop__"))
        raise InvalidCredentialsError("账号或密码错误。")

    _ensure_not_locked_or_disabled(user)

    if not security.verify_password(password or "", user["password_hash"]):
        store.register_login_failure(
            user["id"],
            max_attempts=settings.login_max_attempts,
            lockout_minutes=settings.login_lockout_minutes,
        )
        raise InvalidCredentialsError("账号或密码错误。")

    # 成功：清零失败计数、按需升级哈希、创建会话
    store.reset_login_failure(user["id"])
    if security.needs_rehash(user["password_hash"]):
        store.update_password_hash(user["id"], security.hash_password(password))

    token = security.new_session_token()
    # 记住我 → 更长有效期（Cookie max_age 由 router 按返回的 expires_at 自动跟随）
    ttl_hours = settings.remember_ttl_hours if remember else settings.session_ttl_hours
    expires_at = store.create_session(
        token_hash=security.hash_token(token),
        user_id=user["id"],
        ttl_hours=ttl_hours,
        user_agent=user_agent,
        ip=ip,
    )
    fresh = store.find_user_by_id(user["id"]) or user
    return store.public_user(fresh), token, expires_at


def logout(token: str) -> None:
    """登出：撤销当前会话（token 为明文，内部转哈希查库）。"""
    if token:
        store.revoke_session(security.hash_token(token))


def resolve_session(token: str) -> dict[str, Any] | None:
    """把会话令牌明文解析为「可回传用户信息」；无效/过期/撤销 → None。"""
    if not token:
        return None
    user = store.load_valid_session(security.hash_token(token))
    return store.public_user(user) if user else None


def resolve_session_user(token: str) -> dict[str, Any] | None:
    """把会话令牌解析为「完整用户 dict（含 id，内部用）」；无效 → None。"""
    if not token:
        return None
    return store.load_valid_session(security.hash_token(token))


# --------------------------------------------------------------------------- #
# 个人资料
# --------------------------------------------------------------------------- #
def _require_user(user_id: str) -> dict[str, Any]:
    """取内部完整用户 dict；不存在（会话指向已删账号）→ NotAuthenticatedError。"""
    user = store.find_user_by_id(user_id or "")
    if user is None:
        raise NotAuthenticatedError("登录状态已失效，请重新登录。")
    return user


def _verify_current_password(user: dict[str, Any], password: str) -> None:
    """敏感操作（改密 / 换绑）前置的「当前密码」确认。

    失败同样计入登录失败计数并可能触发锁定：否则这条路径会成为绕过锁定的爆破口子。
    """
    settings = load_settings()
    _ensure_not_locked_or_disabled(user)
    if password and security.verify_password(password, user.get("password_hash") or ""):
        store.reset_login_failure(user["id"])
        return
    store.register_login_failure(
        user["id"],
        max_attempts=settings.login_max_attempts,
        lockout_minutes=settings.login_lockout_minutes,
    )
    raise InvalidCredentialsError("当前密码不正确。")


def get_profile(user_id: str) -> dict[str, Any]:
    """查询完整个人资料"""
    return store.public_user(_require_user(user_id))


def update_profile(user_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    """更新个人资料，返回最新资料

    ``changes`` 只含调用方**显式提交**的键（Pydantic ``exclude_unset``），因此
    「传空值清除」与「不传保持原样」可区分。邮箱 / 手机号 / 密码不在此列，
    必须走 change_email / change_phone / change_password（需当前密码确认）。
    """
    _require_user(user_id)
    fields: dict[str, Any] = {}
    if "nickname" in changes:
        fields["nickname"] = _validate_nickname(changes.get("nickname"))
    if "bio" in changes:
        fields["bio"] = _validate_bio(changes.get("bio"))
    if "gender" in changes:
        fields["gender"] = _validate_gender(changes.get("gender"))
    if "birthday" in changes:
        fields["birthday"] = _validate_birthday(changes.get("birthday"))
    if "location" in changes:
        fields["location"] = _validate_location(changes.get("location"))
    if "website" in changes:
        fields["website"] = _validate_website(changes.get("website"))
    return store.public_user(store.update_user_fields(user_id, **fields) or {})


def change_password(
    user_id: str, current_password: str, new_password: str, *, current_token: str = "",
) -> None:
    """改密：校验当前密码 → 更新哈希 → 踢掉其他设备会话（保留当前会话）。"""
    user = _require_user(user_id)
    _verify_current_password(user, current_password)
    new_password = _validate_password(new_password)
    if security.verify_password(new_password, user.get("password_hash") or ""):
        raise ValidationError("新密码不能与当前密码相同。")
    store.update_password_hash(user_id, security.hash_password(new_password))
    store.revoke_other_sessions(user_id, keep_token=current_token)


def change_email(user_id: str, password: str, new_email: str) -> dict[str, Any]:
    """换绑邮箱：需当前密码确认（防会话被盗后直接改绑定）。

    换绑后 ``email_verified`` 归零——新地址的所有权尚未证明（邮件验证轮次接入）。
    """
    user = _require_user(user_id)
    _verify_current_password(user, password)
    email = identifiers.validate_email(new_email)
    if (user.get("email") or "") == email:
        return store.public_user(user)  # 幂等：改成当前值不报错
    if store.identifier_taken(email=email, exclude_user_id=user_id):
        raise UserExistsError("该邮箱已被其他账号使用。")
    updated = store.update_user_fields(user_id, email=email, email_verified=False)
    return store.public_user(updated or user)


def change_phone(user_id: str, password: str, new_phone: str) -> dict[str, Any]:
    """绑定/换绑手机号：需当前密码确认；传空串表示解绑（但不可与邮箱同时为空）。"""
    user = _require_user(user_id)
    settings = load_settings()
    raw = (new_phone or "").strip()

    if not raw:  # 解绑
        if not (user.get("email") or "").strip():
            raise ValidationError("账号需保留邮箱或手机号其中一项，无法解绑。")
        if not user.get("phone"):
            return store.public_user(user)  # 幂等：本来就没绑
        _verify_current_password(user, password)
        updated = store.update_user_fields(user_id, phone=None, phone_verified=False)
        return store.public_user(updated or user)

    _verify_current_password(user, password)
    phone = identifiers.validate_phone(raw, region=settings.phone_default_region)
    if (user.get("phone") or "") == phone:
        return store.public_user(user)
    if store.identifier_taken(phone=phone, exclude_user_id=user_id):
        raise UserExistsError("该手机号已被其他账号使用。")
    updated = store.update_user_fields(user_id, phone=phone, phone_verified=False)
    return store.public_user(updated or user)


def set_avatar(user_id: str, data: bytes) -> dict[str, Any]:
    """保存头像（校验与落盘见 avatar 模块），返回最新资料。"""
    _require_user(user_id)
    url, _mime = avatar.save_avatar(user_id, data)
    return store.public_user(store.update_user_fields(user_id, avatar_url=url) or {})


def _model_selectable(provider: str, model: str) -> bool:
    """(provider, model) 是否在 ai_models 目录内且已上架（阶段3 DB 化目录）。

    目录读不到（DB 故障）时放行：目录不可用不应锁死用户保存设置——
    运行期由 ai_override_cfg 再兜底（未配 Key 回退全局默认）。
    """
    try:
        from backend.ai import catalog as ai_catalog
        return ai_catalog.is_selectable(provider, model)
    except Exception:
        return True


def update_ai_settings(user_id: str, provider: str | None, model: str | None) -> dict[str, Any]:
    """设置 / 清除「一键 AI 分析」默认模型（provider 与 model 必须同设同清）。

    provider 必须是 ai.config.PROVIDERS 的预设键（平台 Key 池范围，不允许自定义端点）；
    model 必须是 ai_models 目录内且已上架的条目（阶段3 起目录由管理端在线维护，
    下架即不可选），最终仍按预设服务商的固定 base_url 调用——无新增 SSRF 面。
    """
    _require_user(user_id)
    if (provider is None) != (model is None):
        raise ValidationError("服务商与模型必须同时设置或同时清空。")
    model_value: str | None = None
    if provider is not None:
        if provider not in ai_config.PROVIDERS:
            raise ValidationError("不支持的服务商。")
        model_value = (model or "").strip()
        if not model_value or len(model_value) > 128:
            raise ValidationError("模型标识不正确。")
        if not _model_selectable(provider, model_value):
            raise ValidationError("该模型已下架或不在可选目录中。")
    updated = store.update_user_fields(user_id, ai_provider=provider, ai_model=model_value)
    return store.public_user(updated or {})


def remove_avatar(user_id: str) -> dict[str, Any]:
    """删除头像文件并清空 avatar_url（幂等），返回最新资料。"""
    _require_user(user_id)
    avatar.delete_avatar(user_id)
    return store.public_user(store.update_user_fields(user_id, avatar_url=None) or {})
