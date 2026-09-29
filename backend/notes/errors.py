"""notes 异常（语义化 → HTTP 由 router.notes_error_handler 统一映射）。

与 library.errors 同风格：业务层只抛语义化异常，不感知 HTTP 状态码。
冲突（409）是 notes 特有的：自动保存的乐观并发基线（base_updated_at）落后于
库中 updated_at 时抛出，携带服务端版本供前端「加载服务端版本 / 以我的覆盖」二选一。
"""

from __future__ import annotations

from typing import Any


class NotesError(Exception):
    """随手笔记（笔记/图片/抽帧）通用失败基类。"""


class NotFoundError(NotesError):
    """资源不存在或**不属于当前用户** → HTTP 404（不确认资源存在，防 IDOR 探测）。"""


class ValidationError(NotesError):
    """入参非法（正文为空、图片魔数不符、id 非 hex 等）→ HTTP 400。"""


class StorageError(NotesError):
    """磁盘写入/读取失败 → HTTP 500（映射不到更精确语义时的兑底）。"""


class CapExceededError(NotesError):
    """超出上限（笔记数/图片数/图片体积）→ HTTP 400。"""


class ConflictError(NotesError):
    """自动保存冲突：该笔记已在其他窗口被修改 → HTTP 409。

    携带服务端当前版本（note dict），供前端展示冲突二选一。
    """

    def __init__(self, message: str, server: dict[str, Any] | None = None):
        super().__init__(message)
        self.server = server or {}
