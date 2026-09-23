"""通用评论抓取：yt-dlp ``getcomments``（YouTube 等支持评论提取的平台）。

yt-dlp 在开启 ``getcomments`` 后会把评论放进 ``info["comments"]``，每条形如
``{text, author, like_count, timestamp, ...}``。本模块负责把字段归一到门面约定的
``{author, text, likes, time}``，不做排序/截断（由门面统一处理）。

B 站 / 抖音由各自的专用模块接管（yt-dlp 对它们不提供评论），故本模块是兜底。
"""

from __future__ import annotations

from typing import Any

from yt_dlp import YoutubeDL

from backend.comments.errors import CommentsError


def can_handle(url: str) -> bool:
    """兜底解析器：任何 URL 都「能处理」（是否真能取到评论由抓取结果决定）。"""
    return True


def fetch(url: str, limit: int) -> dict[str, Any]:
    """抓取评论并归一化为 {title, comments:[{author,text,likes,time}]}。"""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "getcomments": True,          # 关键：开启评论提取
        "comment_sort": "top",        # 按热度/点赞排序，便于取高赞
        "max_comments": max(int(limit) * 3, int(limit)),  # 上界，避免抓取海量评论
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001 yt-dlp 异常类型繁多，统一兜底
        raise CommentsError(f"yt-dlp 抓取评论失败：{exc}") from exc

    raw = (info or {}).get("comments") or []
    comments: list[dict[str, Any]] = []
    for c in raw:
        text = (c.get("text") or "").strip()
        if not text:
            continue
        comments.append({
            "author": c.get("author") or "匿名",
            "text": text,
            "likes": int(c.get("like_count") or 0),
            "time": c.get("timestamp"),
        })
    return {"title": (info or {}).get("title") or "", "comments": comments}
