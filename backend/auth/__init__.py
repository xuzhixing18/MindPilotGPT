"""认证能力包 —— 多租户身份 / 会话 / 门禁（门面 + 可插拔）。

门面暴露（调用方只需依赖这些符号，不感知 ORM / 哈希 / 存储细节）：
    auth.router                    # FastAPI 路由（/api/auth/*），main.py include_router 即接入
    auth.auth_error_handler        # 语义化异常 → HTTP 的应用级处理器（main.py 注册）
    auth.AuthError                 # 异常基类（含各语义化子类）
    auth.get_current_user          # 依赖：解析请求主体（匿名不抛错）
    auth.require_user              # 依赖：要求已登录（未登录抛 NotAuthenticatedError）
    auth.require_user_if_enabled   # 依赖：业务端点灰度门禁（开关开启且不允许匿名时强制登录）
    auth.CurrentUser               # 请求主体数据类
    auth.register / login / logout # 业务门面（供路由与测试直接调用）
    auth.get_profile / update_profile / change_password / change_email / change_phone
    auth.set_avatar / remove_avatar  # 个人资料与头像门面
    auth.identifiers               # 邮箱/手机号规范化与识别（双通道登录的基础）
    auth.avatar                    # 头像存储（魔数校验 + 本地落盘）
    auth.auth_enabled              # 鉴权总开关（AUTH_ENABLED）
    auth.auth_required             # 业务端点是否强制登录（前后端共用判据）

灰度：``AUTH_ENABLED=false``（默认）时既有开放端点行为不变；``true`` 时按需用
``Depends(require_user)`` 收紧端点。存储层已建 ``users`` / ``sessions`` 两表。

扩展约定（延续「门面 + 可插拔」风格）：
- 阶段B：会话后端换 JWT + Redis（改 session/store，门面不变）；接入 API Key（Bearer）
  解析于 dependencies._token_from_request 并列分支；
- 权益/配额：plans 表与 require_entitlement 依赖在限流轮次接入。
"""

from __future__ import annotations

from backend.auth import avatar, identifiers
from backend.auth.config import auth_enabled, auth_required, load_settings
from backend.auth.dependencies import (
    ANONYMOUS,
    CurrentUser,
    client_ip,
    get_current_user,
    require_user,
    require_user_if_enabled,
)
from backend.auth.errors import (
    AccountDisabledError,
    AccountLockedError,
    AuthError,
    InvalidCredentialsError,
    NotAuthenticatedError,
    StorageError,
    ThrottledError,
    UserExistsError,
    ValidationError,
)
from backend.auth.router import auth_error_handler, router
from backend.auth.service import (
    change_email,
    change_password,
    change_phone,
    get_profile,
    login,
    logout,
    register,
    remove_avatar,
    resolve_session,
    set_avatar,
    update_profile,
)

__all__ = [
    # 路由与异常处理
    "router",
    "auth_error_handler",
    # 依赖与主体
    "get_current_user",
    "require_user",
    "require_user_if_enabled",
    "CurrentUser",
    "ANONYMOUS",
    "client_ip",
    # 配置
    "auth_enabled",
    "auth_required",
    "load_settings",
    # 业务门面
    "register",
    "login",
    "logout",
    "resolve_session",
    # 个人资料与头像
    "get_profile",
    "update_profile",
    "change_password",
    "change_email",
    "change_phone",
    "set_avatar",
    "remove_avatar",
    # 子模块（标识符规范化 / 头像存储）
    "identifiers",
    "avatar",
    # 异常
    "AuthError",
    "ValidationError",
    "UserExistsError",
    "InvalidCredentialsError",
    "AccountLockedError",
    "AccountDisabledError",
    "NotAuthenticatedError",
    "ThrottledError",
    "StorageError",
]
