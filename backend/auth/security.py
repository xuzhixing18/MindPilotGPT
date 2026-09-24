"""认证安全原语：密码哈希、会话令牌生成与哈希、主键 ID 生成。

密码哈希优先 argon2id（业界推荐、抗 GPU/ASIC），未安装 argon2-cffi 时优雅回退到
标准库 PBKDF2-HMAC-SHA256（延续项目「配了更好、没配也能跑」的降级风格）。哈希串自带
方案前缀，故 verify 能自动识别、且将来可无缝升级方案。

会话令牌：高熵不透明随机串（token_urlsafe），DB 只存其 sha256 哈希——即便库泄露也
无法反推可用令牌。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from typing import Any

# argon2 为可选增强：装了用 argon2id，没装回退 PBKDF2（均安全，仅强度/性能有别）
try:
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

    _ARGON2: Any = PasswordHasher()
    _ARGON2_AVAILABLE = True
except ImportError:  # pragma: no cover - argon2-cffi 可选
    _ARGON2 = None
    _ARGON2_AVAILABLE = False

# PBKDF2 回退参数
_PBKDF2_ITERATIONS = 240_000
_PBKDF2_PREFIX = "pbkdf2_sha256"


def argon2_available() -> bool:
    """是否启用了 argon2id（否则使用 PBKDF2 回退）。"""
    return _ARGON2_AVAILABLE


def hash_password(password: str) -> str:
    """把明文密码哈希为可存储串（自带方案前缀，绝不可逆）。"""
    if _ARGON2_AVAILABLE:
        return _ARGON2.hash(password)
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"{_PBKDF2_PREFIX}${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    """校验明文密码与存储哈希是否匹配（自动识别方案；异常一律视为不匹配）。"""
    if not password_hash:
        return False
    if password_hash.startswith("$argon2"):
        if not _ARGON2_AVAILABLE:
            return False  # 库缺失但哈希是 argon2：无法校验，判失败（不误放行）
        try:
            return bool(_ARGON2.verify(password_hash, password))
        except (VerificationError, VerifyMismatchError, InvalidHashError, ValueError):
            return False
    if password_hash.startswith(_PBKDF2_PREFIX + "$"):
        try:
            _scheme, iters, salt_hex, hash_hex = password_hash.split("$", 3)
            dk = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)
            )
        except (ValueError, TypeError):
            return False
        return hmac.compare_digest(dk.hex(), hash_hex)  # 定长比较，抗时序攻击
    return False


def needs_rehash(password_hash: str) -> bool:
    """哈希是否应升级（如 argon2 参数变更，或历史 PBKDF2 而现已装 argon2）。"""
    if _ARGON2_AVAILABLE and password_hash.startswith("$argon2"):
        try:
            return bool(_ARGON2.check_needs_rehash(password_hash))
        except (InvalidHashError, ValueError):
            return True
    # 已装 argon2 但存的是 PBKDF2 → 建议升级
    return _ARGON2_AVAILABLE and password_hash.startswith(_PBKDF2_PREFIX + "$")


def new_id() -> str:
    """生成 32 位十六进制主键（uuid4）。"""
    return uuid.uuid4().hex


def new_session_token() -> str:
    """生成高熵不透明会话令牌（原始值只进 Cookie，DB 存其哈希）。"""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """会话令牌的 sha256 hex（作为 sessions 表主键，避免明文落库）。"""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()
