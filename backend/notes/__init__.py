"""notes 能力包 —— 随手笔记（笔记 / 图片 / 抽帧）门面。

门面暴露（调用方只需依赖这些符号，不感知 ORM 细节）：
    notes.router                  # FastAPI 路由（/api/me/notes/*），main.py include_router 接入
    notes.notes_error_handler     # 语义化异常 → HTTP 的应用级处理器（main.py 注册）
    notes.NotesError              # 异常基类（NotFoundError / ValidationError / CapExceededError / ConflictError）
    notes.service                 # 业务门面（CRUD / 图片 / 冲突检测 / marks 解析）
    notes.config                  # 上限与目录配置（env 驱动）
    notes.set_stream_cache_lookup # 流播缓存查询回调注入（main.py 启动时调用）

为什么是独立包而非塞进 library（架构决策，详见 docs/随手笔记功能设计方案.md §3）：
library 的语义是「归属与时间线」（历史/合集/会话，只记引用不存内容），笔记是
**内容本体**（正文/图片/版本/冲突保护），职责与上限策略完全不同；
混入会让 library/store.py 继续膨胀。两者只共享两条约定：user_id 列级隔离 + content_key
视频身份键；历史 kinds 联动经 library 既有钩子（record_action / history_remove_kind）。

隔离约定：私有资源**永远**要求登录（Depends(require_user)，不看 AUTH_ENABLED 开关）；
store 层所有函数首参 user_id，查询强制列级过滤；跨用户访问统一 404。
"""

from __future__ import annotations

from backend.notes import config, service, store
from backend.notes.errors import (
    CapExceededError,
    ConflictError,
    NotesError,
    NotFoundError,
    StorageError,
    ValidationError,
)
from backend.notes.router import notes_error_handler, router, set_stream_cache_lookup

__all__ = [
    # 路由与异常处理
    "router",
    "notes_error_handler",
    "set_stream_cache_lookup",
    # 异常
    "NotesError",
    "NotFoundError",
    "ValidationError",
    "CapExceededError",
    "ConflictError",
    "StorageError",
    # 业务门面
    "service",
    "store",
    "config",
]
