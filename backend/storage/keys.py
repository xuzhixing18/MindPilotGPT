"""缓存键与 URL 规范化。

缓存命中率的关键在于「同一视频的不同链接形式」能归一到同一个键：
- 去掉跟踪/分享类查询参数（utm_*、spm、share_* 等），保留真正标识视频的参数
  （如 YouTube 的 ``?v=``）；
- scheme/host 小写、去 fragment、query 排序，避免顺序差异导致键不同。

规范化只在「能拿到的输入 URL」上做（查缓存必须在真正抓取之前），因此抖音短链
与其跳转后的规范地址仍可能各存一份——这是可接受的少量冗余。
"""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# 跟踪/分享类参数：精确名（小写）
_TRACKING_EXACT = {
    "spm", "from", "gclid", "fbclid", "ref", "t", "timestamp",
    "unique_k", "buvid", "is_story_feed", "share_token", "session_id",
    "pl", "pt", "vd_source", "spm_id_from", "from_source", "share_from",
}
# 跟踪/分享类参数：前缀（小写）
_TRACKING_PREFIXES = ("utm_", "share_", "vd_", "spm", "from_", "fb_", "ig_")


def _is_tracking(name: str) -> bool:
    k = (name or "").lower()
    if not k:
        return False
    if k in _TRACKING_EXACT:
        return True
    return any(k.startswith(p) for p in _TRACKING_PREFIXES)


def normalize_url(url: str) -> str:
    """把 URL 归一化为缓存键基础串：小写 scheme/host、去 fragment、丢跟踪参数、query 排序。"""
    url = (url or "").strip()
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    scheme = parts.scheme.lower()
    host = parts.netloc.lower()
    path = parts.path or "/"
    kept = [
        (k, v)
        for (k, v) in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking(k)
    ]
    kept.sort()
    query = urlencode(kept)
    return urlunsplit((scheme, host, path, query, ""))


def transcript_key(url: str) -> str:
    """转写缓存键：规范化 URL 的 sha256。"""
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()


def summary_key(text: str, model: str, prompt_version: str) -> str:
    """总结缓存键：文本 + 模型 + 提示词版本 的 sha256（任一变化即自然失效）。"""
    raw = f"{text or ''}\x1f{model or ''}\x1f{prompt_version or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
