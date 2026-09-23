"""B 站评论抓取：BV/av → aid → ``x/v2/reply/main``（mode=3 按点赞热度）。

B 站评论不在 yt-dlp 支持范围内，走公开 web 接口：
1) 用 ``x/web-interface/view`` 由 bvid/aid 拿到 aid 与标题（同时兼容 b23.tv 短链）；
2) 用 ``x/v2/reply/main``（``mode=3`` 即热门/按赞）取首页评论。
读评论无需登录；字段归一到 ``{author, text, likes, time}``。
"""

from __future__ import annotations

import re
from typing import Any

import requests

from backend.comments.errors import CommentsError, CommentsNotSupportedError

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}
_TIMEOUT = 20


def can_handle(url: str) -> bool:
    u = (url or "").lower()
    return "bilibili.com" in u or "b23.tv" in u


def _resolve(url: str) -> tuple[int, str]:
    """解析出 (aid, title)；支持 b23.tv 短链、BV 号与 av 号。"""
    try:
        if "b23.tv" in url.lower():
            url = requests.get(url, headers=_HEADERS, timeout=15, allow_redirects=True).url
    except requests.RequestException as exc:
        raise CommentsError(f"B 站短链解析失败：{exc}") from exc

    params: dict[str, Any] = {}
    av = re.search(r"av(\d+)", url)
    bv = re.search(r"(BV[0-9A-Za-z]{10})", url)
    if av:
        params["aid"] = int(av.group(1))
    elif bv:
        params["bvid"] = bv.group(1)
    else:
        raise CommentsNotSupportedError("无法从链接中识别 B 站视频 ID。")

    try:
        resp = requests.get(
            "https://api.bilibili.com/x/web-interface/view",
            params=params, headers=_HEADERS, timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = (resp.json() or {}).get("data") or {}
    except (requests.RequestException, ValueError) as exc:
        raise CommentsError(f"B 站视频信息获取失败：{exc}") from exc

    aid = data.get("aid")
    if not aid:
        raise CommentsNotSupportedError("未能获取 B 站视频 aid。")
    return int(aid), data.get("title") or ""


def fetch(url: str, limit: int) -> dict[str, Any]:
    """抓取评论并归一化为 {title, comments:[{author,text,likes,time}]}。"""
    aid, title = _resolve(url)
    try:
        resp = requests.get(
            "https://api.bilibili.com/x/v2/reply/main",
            params={
                "type": 1, "oid": aid, "mode": 3,
                "ps": min(max(int(limit), 1), 49), "next": 0,
            },
            headers=_HEADERS, timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = (resp.json() or {}).get("data") or {}
    except (requests.RequestException, ValueError) as exc:
        raise CommentsError(f"B 站评论获取失败：{exc}") from exc

    comments: list[dict[str, Any]] = []
    for r in (data.get("replies") or []):
        text = ((r.get("content") or {}).get("message") or "").strip()
        if not text:
            continue
        comments.append({
            "author": ((r.get("member") or {}).get("uname")) or "匿名",
            "text": text,
            "likes": int(r.get("like") or 0),
            "time": r.get("ctime"),
        })
    return {"title": title, "comments": comments}
