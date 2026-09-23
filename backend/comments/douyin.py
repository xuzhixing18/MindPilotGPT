"""抖音评论抓取：复用 ``downloader.douyin`` 的视频 ID 解析 + iesdouyin 评论接口

抖音视频 ID 解析（含短链跳转）已在 downloader.douyin 实现，这里直接复用其
``can_handle`` / ``extract_first_url`` / ``resolve_video_id``，只额外调评论接口。
抖音 web 评论接口对签名/风控较敏感，失败时抛 CommentsError，由门面转成友好提示。
"""

from __future__ import annotations

from typing import Any

import requests

from backend.comments.errors import CommentsError
from backend.downloader import douyin as _douyin

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://www.douyin.com/",
}
_TIMEOUT = 20


def can_handle(url: str) -> bool:
    return _douyin.can_handle(url)


def fetch(url: str, limit: int) -> dict[str, Any]:
    """抓取评论并归一化为 {title, comments:[{author,text,likes,time}]}。"""
    try:
        video_id, _resolved = _douyin.resolve_video_id(_douyin.extract_first_url(url))
    except Exception as exc:  # noqa: BLE001 解析器内部异常类型多样，统一兜底
        raise CommentsError(f"抖音视频 ID 解析失败：{exc}") from exc

    try:
        resp = requests.get(
            "https://www.iesdouyin.com/web/api/v2/comment/list/",
            params={
                "item_id": video_id,
                "cursor": 0,
                "count": min(max(int(limit), 1), 50),
            },
            headers=_HEADERS, timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json() or {}
    except (requests.RequestException, ValueError) as exc:
        raise CommentsError(f"抖音评论获取失败：{exc}") from exc

    comments: list[dict[str, Any]] = []
    for c in (data.get("comments") or []):
        text = (c.get("text") or "").strip()
        if not text:
            continue
        comments.append({
            "author": ((c.get("user") or {}).get("nickname")) or "匿名",
            "text": text,
            "likes": int(c.get("digg_count") or 0),
            "time": c.get("create_time"),
        })
    return {"title": "", "comments": comments}
