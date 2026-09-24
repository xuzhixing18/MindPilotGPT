"""认证异常（与转写/总结/评论的异常语义对齐，便于 router/main.py 统一映射 HTTP 状态）。"""

from __future__ import annotations


class AuthError(Exception):
    """认证通用失败基类。"""


class ValidationError(AuthError):
    """入参非法（邮箱/手机号格式错误、密码过弱、头像格式不支持等）→ HTTP 400。"""


class UserExistsError(AuthError):
    """邮箱或手机号已被注册 → HTTP 409。"""


class InvalidCredentialsError(AuthError):
    """账号或密码错误 → HTTP 401（对外不区分「用户不存在」与「密码错误」，防枚举）。"""


class AccountLockedError(AuthError):
    """账号因多次失败被临时锁定 → HTTP 423。"""


class AccountDisabledError(AuthError):
    """账号被禁用 → HTTP 403。"""


class NotAuthenticatedError(AuthError):
    """需要登录但未认证 / 会话失效 → HTTP 401。"""


class ThrottledError(AuthError):
    """登录过于频繁（IP 级限流）→ HTTP 429。携带 retry_after 秒数。"""

    def __init__(self, message: str = "登录尝试过于频繁，请稍后再试。", retry_after: int = 60) -> None:
        super().__init__(message)
        self.retry_after = int(retry_after)


class StorageError(AuthError):
    """头像等文件落盘失败（目录不可写 / 磁盘满）→ HTTP 500。

    对外不暴露路径等细节（防信息泄露），具体原因只进日志。
    """
