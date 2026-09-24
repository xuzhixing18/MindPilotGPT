"""library 异常（语义化 → HTTP 由 router.library_error_handler 统一映射）。

与 auth.errors 同风格：业务层只抛语义化异常，不感知 HTTP 状态码。
"""

from __future__ import annotations


class LibraryError(Exception):
    """私有资源（历史/合集/问答会话）通用失败基类。"""


class NotFoundError(LibraryError):
    """资源不存在或**不属于当前用户** → HTTP 404（不确认资源存在，防 IDOR 探测）。"""


class ValidationError(LibraryError):
    """入参非法（合集名为空/超长、重复入集等）→ HTTP 400。"""


class CapExceededError(LibraryError):
    """超出上限（合集数/条目数/历史数）→ HTTP 400。"""
