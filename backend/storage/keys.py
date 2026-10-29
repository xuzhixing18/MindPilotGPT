"""缓存键与 URL 规范化。

缓存命中率的关键在于「同一视频的不同链接形式」能归一到同一个键：
- 去掉跟踪/分享类查询参数（utm_*、spm、share_* 等），保留真正标识视频的参数
  （如 YouTube 的 ``?v=``）；
- scheme/host 小写、query 排序；fragment 默认丢弃，但 SPA 哈希路由站点
  （无真实 path 且 fragment 以 / 开头，如网易云 music.163.com/#/mv?id=）
  把 fragment 的路由段并入 path/query，避免不同视频塌缩成同一个 host 级键。

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
    """把 URL 归一化为缓存键基础串：小写 scheme/host、丢跟踪参数、query 排序。

    fragment 处理：普通页面锚点（#section、带 path 的 #/route）直接丢弃；
    但无真实 path 且 fragment 以 / 开头时是 SPA 哈希路由（网易云等），
    路由与 id 都在 fragment 里，必须并入 path/query——否则
    ``https://music.163.com/#/mv?id=1`` 与 ``#/mv?id=2`` 会合并成同一个
    host 级缓存键，命中旧视频的缓存串台返回。
    """
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
    query = parts.query
    fragment = parts.fragment
    if fragment.startswith("/") and path in ("", "/"):
        # 哈希路由并入真实路径：与不带 # 的等价链接归一一致，消除双写键
        frag_path, _, frag_query = fragment.partition("?")
        path = frag_path or "/"
        if frag_query:
            query = f"{query}&{frag_query}" if query else frag_query
    kept = [
        (k, v)
        for (k, v) in parse_qsl(query, keep_blank_values=True)
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


def mindmap_key(text: str, model: str, prompt_version: str) -> str:
    """思维导图缓存键：与 summary_key 同理，加域前缀避免与总结键空间混淆。"""
    raw = f"mindmap\x1f{text or ''}\x1f{model or ''}\x1f{prompt_version or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def comments_key(url: str) -> str:
    """评论缓存键：域前缀 + 规范化 URL 的 sha256（与转写键空间隔离）。"""
    raw = f"comments\x1f{normalize_url(url)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
