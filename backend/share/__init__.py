"""share 能力包 —— 分享（短链落地页 / 海报数据 / 多渠道分发支撑）门面。

门面暴露（调用方只需依赖这些符号，不感知 ORM 细节）：
    share.router                 # FastAPI 路由（私有 /api/me/shares/* + 公开 /s/{code} 等）
    share.share_error_handler    # 语义化异常 → HTTP 的应用级处理器（main.py 注册）
    share.ShareError             # 异常基类（NotFound / Gone / Validation / Throttled / Storage）
    share.service                # 业务门面（幂等创建 / 快照消毒 / 公开白名单 / 限流与浏览去重）
    share.config                 # 短链 / 限流 / 快照上限配置（env 驱动）
    share.landing                # 落地页 SSR 渲染（og meta + 微信 title/首图兜底）

为什么是独立包（架构决策，详见 docs/分享功能设计方案.md §4）：分享是**显式公开
动作**，与 library/notes 的私有语义相反——公开端点不挂门禁、响应字段白名单、
限流与浏览去重都是独立职责；与两者只共享 content_key 身份键一条约定。
快照在创建时拼装落库（全局缓存过期后落地页仍可读），海报渲染留给前端 canvas
（服务端无 CJK 字体），本包只提供海报所需数据（同源封面代理 + QR SVG）。
"""

from __future__ import annotations

from backend.share import config, landing, service, store
from backend.share.errors import (
    GoneError,
    NotFoundError,
    ShareError,
    StorageError,
    ThrottledError,
    ValidationError,
)
from backend.share.router import router, share_error_handler

__all__ = [
    # 路由与异常处理
    "router",
    "share_error_handler",
    # 异常
    "ShareError",
    "NotFoundError",
    "GoneError",
    "ValidationError",
    "ThrottledError",
    "StorageError",
    # 业务门面
    "service",
    "store",
    "config",
    "landing",
]
