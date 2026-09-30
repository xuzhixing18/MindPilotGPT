"""share 语义化异常：与 notes/errors.py 同范式（路由不写状态码，处理器统一映射）。"""

from __future__ import annotations


class ShareError(Exception):
    """share 包异常基类（应用级处理器按 MRO 映射 HTTP）。"""


class NotFoundError(ShareError):
    """分享不存在或不属于当前用户（统一 404，不确认存在）。"""


class GoneError(ShareError):
    """分享已被创建者撤销（410，落地页展示撤销文案）。"""


class ValidationError(ShareError):
    """入参不合法（kind 未知 / QR 数据域外 / 快照超限等，400）。"""


class ThrottledError(ShareError):
    """公开端点限流（429）。"""

    def __init__(self, message: str = "请求过于频繁，请稍后再试。", retry_after: int = 60) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class StorageError(ShareError):
    """存储层故障（500）。"""


__all__ = [
    "ShareError",
    "NotFoundError",
    "GoneError",
    "ValidationError",
    "ThrottledError",
    "StorageError",
]
