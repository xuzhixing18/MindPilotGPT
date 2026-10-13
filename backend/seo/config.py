"""seo 配置：站点级 TDK 常量与 sitemap 上限（env 驱动，与 share/config.py 同范式）。

``PUBLIC_BASE_URL`` 是 canonical / og:url / robots Sitemap / sitemap.xml 的域名
事实源（与 share 包共用同一 env）：反代 / https 部署必须配置，否则回退请求
base_url（可能编码成内网地址，仅本地开发可接受）。
"""

from __future__ import annotations

import os

SITE_NAME = "MindPilot"
SITE_TITLE = "MindPilot - 万能视频下载与AI分析工具 | 字幕转写·AI总结·思维导图"
SITE_DESCRIPTION = (
    "MindPilot 是一站式视频下载与 AI 分析工具：粘贴链接即可高速下载 B站、YouTube、"
    "抖音、小红书、TikTok 等 20+ 平台视频，自动提取字幕并生成 AI 总结、思维导图、"
    "高赞评论与多轮问答，支持随手笔记和一键分享，立即免费体验。"
)

# index.html 中的占位符：seo 路由渲染 "/" 与 "/index.html" 时替换为站点绝对地址
BASE_URL_TOKEN = "__SEO_BASE_URL__"


def _int(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or str(default)).strip())
    except ValueError:
        return default


def public_base_url() -> str:
    """站点域名事实源（env 优先，去尾斜杠）；未配置时由 router 按请求 base_url 回退。"""
    return (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")


def sitemap_share_limit() -> int:
    """sitemap.xml 收录的公开分享条数上限（默认 2000；单文件协议上限 50000）。"""
    return max(0, _int("SITEMAP_SHARE_LIMIT", 2000))
