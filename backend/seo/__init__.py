"""seo 能力包 —— 站点级搜索引擎基础设施门面。

门面暴露（调用方只需依赖这些符号，与 notes/share 包同范式）：
    seo.router                  # FastAPI 路由（/robots.txt、/sitemap.xml、/ 与 /index.html 动态渲染）
    seo.http_exception_handler  # StarletteHTTPException 处理器（非 /api 404 → 品牌 HTML 页）
    seo.config                  # 站点 TDK 常量 / PUBLIC_BASE_URL 事实源 / sitemap 上限

为什么是独立包（对齐工作流 docs/SEO优化工作流.md）：robots / sitemap / canonical
注入 / 404 治理是「站点对爬虫的契约」，与业务包无耦合；唯一依赖是 share.store 的
公开分享列表（sitemap 收录 /s/{code} 落地页——站内唯一可被收录的 SSR 内容页）。
main.py 须在 StaticFiles mount **之前**注册本包路由（"/" 由本包接管）。
"""

from __future__ import annotations

from backend.seo import config
from backend.seo.router import http_exception_handler, router

__all__ = [
    "router",
    "http_exception_handler",
    "config",
]
