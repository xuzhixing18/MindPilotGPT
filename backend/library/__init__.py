"""library 能力包 —— 用户私有资源（处理历史 / 合集 / 问答会话）门面。

门面暴露（调用方只需依赖这些符号，不感知 ORM 细节）：
    library.router                  # FastAPI 路由（/api/me/*），main.py include_router 接入
    library.library_error_handler   # 语义化异常 → HTTP 的应用级处理器（main.py 注册）
    library.LibraryError            # 异常基类（NotFoundError / ValidationError / CapExceededError）
    library.record_action           # 记录钩子：内容端点成功后异步写历史（BackgroundTask）
    library.ask_in_session          # QA 会话编排：DB 历史组装 + 持久化本轮问答
    library.service                 # 业务门面（历史/合集/会话/侧边栏聚合）
    library.config                  # 上限配置（env 驱动）

隔离约定：私有资源**永远**要求登录（``Depends(require_user)``，不看 AUTH_ENABLED 开关）；
store 层所有函数首参 user_id，查询强制列级过滤；跨用户访问/删除统一 404。
内容缓存（transcripts/summaries/...）保持全局共享不动，本包只记归属与时间线。
"""

from __future__ import annotations

from backend.library import config, service, store
from backend.library.errors import CapExceededError, LibraryError, NotFoundError, ValidationError
from backend.library.router import library_error_handler, router
from backend.library.service import ask_in_session, record_action

__all__ = [
    # 路由与异常处理
    "router",
    "library_error_handler",
    # 异常
    "LibraryError",
    "NotFoundError",
    "ValidationError",
    "CapExceededError",
    # 业务门面
    "record_action",
    "ask_in_session",
    "service",
    "store",
    "config",
]
