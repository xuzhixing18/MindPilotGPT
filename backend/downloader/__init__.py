"""视频解析下载包 —— 对外统一门面与调度器。

调用方（FastAPI）只需：
    from backend import downloader
    downloader.extract_info(url)          # 解析视频信息
    downloader.download(url, format_id)   # 服务端下载
    downloader.ffmpeg_available()         # ffmpeg 能力（影响高清合并）

调度规则：按 registry.SPECIFIC_EXTRACTORS 顺序用 can_handle(url) 匹配专用解析器
（如抖音 douyin），都不命中则回退到 generic（基于 yt-dlp 的通用解析器）。
新增平台无需改动本文件，只在 registry 注册即可。
"""

from __future__ import annotations

from typing import Any

from backend.downloader import generic, registry
from backend.downloader.common import ffmpeg_available

__all__ = ["extract_info", "download", "ffmpeg_available"]


def _pick(url: str):
    """按注册表选出处理该 url 的解析器模块：命中专用则用之，否则回退通用。"""
    for mod in registry.SPECIFIC_EXTRACTORS:
        if mod.can_handle(url):
            return mod
    return generic


def extract_info(url: str) -> dict[str, Any]:
    """解析视频信息（自动路由到对应平台解析器）。"""
    return _pick(url).extract_info(url)


def download(url: str, format_id: str | None = None) -> dict[str, Any]:
    """下载视频（自动路由到对应平台解析器）。"""
    return _pick(url).download(url, format_id)
