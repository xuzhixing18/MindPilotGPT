"""FastAPI 认证依赖：把请求解析为 CurrentUser，并提供 require_user 门禁。

- ``get_current_user``：从会话 Cookie 解析用户；解析不到（或 AUTH_ENABLED=false）时返回
  **匿名主体**（不抛错），让既有开放端点在灰度期零改动继续工作。
- ``require_user``：在 get_current_user 之上要求「必须已登录」，否则抛 NotAuthenticatedError
  （router/main 映射 401）。需要鉴权的端点用 ``Depends(require_user)`` 声明即可。
- ``require_user_if_enabled``：业务端点用的**灰度门禁**——仅当 AUTH_ENABLED=true 且不允许
  匿名时才强制登录；开关关闭时直接放行，既有端点行为零改变。

阶段A 只识别会话 Cookie；API Key（Bearer）解析在 M4 轮次接入，届时在此扩展而不改调用方。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import Depends, Request

from backend.auth import service
from backend.auth.config import auth_required, load_settings
from backend.auth.errors import NotAuthenticatedError


@dataclass(frozen=True)
class CurrentUser:
    """请求主体：已登录用户或匿名。"""

    user_id: str | None = None
    email: str | None = None
    nickname: str | None = None
    plan_id: str = "free"
    ai_provider: str | None = None
    ai_model: str | None = None
    is_authenticated: bool = False
    scopes: tuple[str, ...] = field(default=())

    @property
    def is_anonymous(self) -> bool:
        return not self.is_authenticated


# 匿名主体单例（不可变，可安全复用）
ANONYMOUS = CurrentUser(is_authenticated=False)


def client_ip(request: Request) -> str:
    """取客户端 IP：优先可信代理写入的 X-Forwarded-For 首个地址，否则用直连地址。"""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


def _token_from_request(request: Request) -> str:
    """从会话 Cookie 取令牌明文（阶段A）；将来可在此并列解析 Bearer API Key。"""
    settings = load_settings()
    return request.cookies.get(settings.cookie_name, "") or ""


def get_current_user(request: Request) -> CurrentUser:
    """解析当前请求主体；无有效会话时返回匿名（不抛错）。"""
    token = _token_from_request(request)
    if not token:
        return ANONYMOUS
    user: dict[str, Any] | None = service.resolve_session_user(token)
    if user is None:
        return ANONYMOUS
    return CurrentUser(
        user_id=user["id"],
        email=user.get("email"),
        nickname=user.get("nickname"),
        plan_id=user.get("plan_id") or "free",
        ai_provider=user.get("ai_provider") or None,
        ai_model=user.get("ai_model") or None,
        is_authenticated=True,
    )


def require_user(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """门禁依赖：**无条件**要求已登录，否则抛 NotAuthenticatedError（→ 401）。

    用法：``@router.get(...)`` 的函数签名里加 ``user: CurrentUser = Depends(require_user)``。
    """
    if not user.is_authenticated:
        raise NotAuthenticatedError("请先登录。")
    return user


def require_user_if_enabled(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """业务端点门禁：按灰度开关决定是否强制登录（既有开放行为可零改动保留）。

    三态语义：
    - ``AUTH_ENABLED=false``                      → 放行（匿名），与改造前完全一致；
    - ``AUTH_ENABLED=true`` + ``ALLOW_ANONYMOUS`` → 放行（匿名走免费额度/更严限流）；
    - ``AUTH_ENABLED=true`` + 不允许匿名        → **必须登录**，否则 401。

    与 ``require_user`` 的区别：后者不看开关，用于「无论如何都要登录」的端点（如 /me）。
    """
    if not auth_required():
        return user
    if not user.is_authenticated:
        raise NotAuthenticatedError("请先登录后再使用。")
    return user


def ai_override_cfg(user: CurrentUser):
    """解析登录用户的「默认模型」覆盖配置（供 AI 业务端点使用）。

    返回 None（跟随全局 env）的三种情形：
    1. 未登录 / 未设置 ai_provider；
    2. 所选模型已从 ai_models 目录下架或被删除（阶段3 管理端在线运营，
       下架即时生效——已存该模型的用户自动回退，无需迁移数据）；
    3. 所选服务商未配 Key。
    延迟导入 ai.* 以避免包间耦合（auth 不硬依赖 ai 能力包）。
    """
    if not user.is_authenticated or not user.ai_provider:
        return None
    from backend.ai import catalog as ai_catalog
    from backend.ai import config as ai_config

    if user.ai_model and not ai_catalog.is_selectable(user.ai_provider, user.ai_model):
        return None
    return ai_config.load_config(provider=user.ai_provider, model=user.ai_model)
