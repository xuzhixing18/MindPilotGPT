"""认证配置：环境变量驱动（延续 ai/config.py、asr.py 的「配置驱动」范式）。

阶段A 覆盖会话 Cookie 认证、登录限流/锁定与个人资料（手机号区号、头像上传限制）；
JWT 签名密钥（SESSION_SECRET）等阶段B 能力预留读取但当前非必需（不透明会话令牌无需签名）。

安全：敏感项只从环境变量 / .env 读取，绝不硬编码。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# 项目根目录（backend/auth/config.py → auth → backend → root）
_ROOT = Path(__file__).resolve().parents[2]

# 头像默认存储目录（可经 AVATAR_DIR 覆盖，如指向挂载的对象存储同步目录）
DEFAULT_AVATAR_DIR = _ROOT / "data" / "avatars"

# 尽力加载 .env（未安装 python-dotenv 时静默跳过，仍可用系统环境变量）
try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")
except ImportError:  # pragma: no cover - python-dotenv 可选
    pass

_FALSEY = {"0", "false", "no", "off", ""}
_TRUTHY = {"1", "true", "yes", "on"}


def _bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    if raw in _TRUTHY:
        return True
    if raw in _FALSEY:
        return False
    return default


def _int(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or "").strip() or default)
    except ValueError:
        return default


def _avatar_dir() -> Path:
    """头像目录：相对路径按项目根解析，绝对路径直接用。"""
    raw = (os.getenv("AVATAR_DIR") or "").strip()
    if not raw:
        return DEFAULT_AVATAR_DIR
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (_ROOT / p).resolve()


@dataclass(frozen=True)
class AuthSettings:
    """一次运行期的认证相关配置快照。"""

    enabled: bool              # AUTH_ENABLED：总开关，false 时保持既有「无鉴权」行为
    allow_anonymous: bool      # AUTH_ALLOW_ANONYMOUS：true 时匿名仍可访问（走 IP 限流）
    cookie_name: str
    cookie_secure: bool
    cookie_samesite: str       # lax / strict / none
    cookie_path: str
    session_ttl_hours: int     # 普通登录的会话有效期
    remember_ttl_hours: int    # 勾选「记住我」时的更长有效期（前端 30 天免登录）
    # 登录失败锁定
    login_max_attempts: int
    login_lockout_minutes: int
    # 登录端点 IP 级限流（阶段A 内置轻量实现，完整 ratelimit 包上线后由策略表接管）
    login_ip_max_per_window: int
    login_ip_window_seconds: int
    password_min_length: int
    # 登录标识符：手机号缺省补的国家/地区码（无 + 前缀时生效）
    phone_default_region: str
    # 个人资料
    nickname_max_length: int
    bio_max_length: int
    avatar_dir: Path           # 头像落盘目录
    avatar_max_bytes: int      # 单张头像体积上限


def load_settings() -> AuthSettings:
    """从环境变量解析认证配置（每次调用实时读取，便于测试与灰度切换）。"""
    samesite = (os.getenv("SESSION_COOKIE_SAMESITE") or "lax").strip().lower()
    if samesite not in ("lax", "strict", "none"):
        samesite = "lax"
    return AuthSettings(
        enabled=_bool("AUTH_ENABLED", False),
        allow_anonymous=_bool("AUTH_ALLOW_ANONYMOUS", True),
        cookie_name=(os.getenv("SESSION_COOKIE_NAME") or "mp_sid").strip() or "mp_sid",
        cookie_secure=_bool("SESSION_COOKIE_SECURE", False),
        cookie_samesite=samesite,
        cookie_path=(os.getenv("SESSION_COOKIE_PATH") or "/").strip() or "/",
        session_ttl_hours=_int("SESSION_TTL_HOURS", 168),
        remember_ttl_hours=_int("REMEMBER_TTL_HOURS", 720),
        login_max_attempts=_int("LOGIN_MAX_ATTEMPTS", 5),
        login_lockout_minutes=_int("LOGIN_LOCKOUT_MINUTES", 15),
        login_ip_max_per_window=_int("LOGIN_IP_MAX_PER_WINDOW", 10),
        login_ip_window_seconds=_int("LOGIN_IP_WINDOW_SECONDS", 60),
        password_min_length=_int("PASSWORD_MIN_LENGTH", 8),
        phone_default_region=(os.getenv("PHONE_DEFAULT_REGION") or "CN").strip().upper() or "CN",
        nickname_max_length=_int("NICKNAME_MAX_LENGTH", 32),
        bio_max_length=_int("BIO_MAX_LENGTH", 200),
        avatar_dir=_avatar_dir(),
        avatar_max_bytes=_int("AVATAR_MAX_BYTES", 2 * 1024 * 1024),
    )


def auth_enabled() -> bool:
    """鉴权总开关是否开启。"""
    return load_settings().enabled


def auth_required() -> bool:
    """业务端点是否**强制登录**：开关开启且不允许匿名。

    作为前后端共用的单一判据：后端门禁（require_user_if_enabled）与 /api/health
    暴露给前端的拦截开关均取此值，避免两边判定不一致。
    """
    settings = load_settings()
    return settings.enabled and not settings.allow_anonymous
