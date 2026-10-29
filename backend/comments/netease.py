"""网易云音乐评论抓取：mv/video/song/album/playlist → ``api/v1/resource/comments/{资源ID}``。

网易云评论不在 yt-dlp 支持范围内（generic 兜底永远拿到空列表），走公开旧接口：
``https://music.163.com/api/v1/resource/comments/R_MV_5_14579633?limit=&offset=``
**无需签名/Cookie**（weapi/eapi 新接口才需要 AES/RSA 加密，别走那些）；实测匿名
可用，返回 ``hotComments``（热度榜）+ ``comments``（最新页）+ ``topComments``（置顶）。

链接的真实路由在 fragment 里（``#/mv?id=``），资源前缀映射：
``mv→R_MV_5``、``video→R_VI_62``、``song→R_SO_4``、``album→R_AL_3``、``playlist→A_PL_0``。
MV 标题经 ``api/mv/detail?id=``、歌曲经 ``api/song/detail?ids=[id]`` 补全
（失败静默留空——标题仅导出用，评论才是核心）。

接口 ``code != 200`` 时抛 ``CommentsError`` 如实上报，只有 code=200 且确无评论才返回
空列表交给门面判 NotSupported（与 bilibili 同一红线：不得伪装「不支持」）。
字段归一：``content/likedCount/time``(毫秒)/``user.nickname`` → ``author/text/likes/time``(秒)。
"""

from __future__ import annotations

import re
from typing import Any

import requests

from backend.comments.errors import CommentsError

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://music.163.com/",
    "Accept": "application/json, text/plain, */*",
}
_TIMEOUT = 20

# hash 路由 kind → 评论资源前缀（fragment 才是真实路由，如 #/mv?id=14579633）
_RESOURCE = {
    "mv": "R_MV_5",
    "video": "R_VI_62",
    "song": "R_SO_4",
    "album": "R_AL_3",
    "playlist": "A_PL_0",
}
_ROUTE_RE = re.compile(r"(?:#/)?(mv|video|song|album|playlist)\?id=(\d+)")


def can_handle(url: str) -> bool:
    return "music.163.com" in (url or "").lower()


def _get_json(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """统一 GET + JSON 解析（测试可 monkeypatch 本函数隔离网络）。"""
    try:
        resp = requests.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json() or {}
    except requests.RequestException as exc:
        raise CommentsError(f"网易云接口请求失败：{exc}") from exc
    except ValueError as exc:  # 非 JSON 响应：多为风控拦截页
        raise CommentsError(f"网易云接口响应非 JSON（可能被风控拦截）：{exc}") from exc


def _extract_route(url: str) -> tuple[str, str, int]:
    """从链接提取 (评论资源前缀, kind, 资源ID)；hash 路由与直链都识别。"""
    m = _ROUTE_RE.search(url or "")
    if not m:
        raise CommentsError("无法从链接中识别网易云 MV/视频/歌曲/专辑/歌单 ID。")
    kind, raw_id = m.group(1), m.group(2)
    return _RESOURCE[kind], kind, int(raw_id)


def _resolve_title(kind: str, rid: int) -> str:
    """补全标题：mv/song 有公开 detail 接口；失败静默留空（标题仅导出用）。"""
    try:
        if kind == "mv":
            data = (_get_json("https://music.163.com/api/mv/detail", {"id": rid}) or {}).get("data") or {}
            name = data.get("name") or ""
            artist = data.get("artistName") or ""
            return f"{name} - {artist}" if name and artist else name
        if kind == "song":
            data = _get_json("https://music.163.com/api/song/detail", {"ids": f"[{rid}]"}) or {}
            songs = data.get("songs") or []
            if songs:
                return songs[0].get("name") or ""
    except CommentsError:
        pass
    return ""


def fetch(url: str, limit: int) -> dict[str, Any]:
    """抓取评论并归一化为 {title, comments:[{author,text,likes,time}]}。"""
    resource, kind, rid = _extract_route(url)
    data = _get_json(
        f"https://music.163.com/api/v1/resource/comments/{resource}_{rid}",
        {"limit": min(max(int(limit), 1), 50), "offset": 0},
    )
    # 注意 code=200 是成功值，不能用 `or` 兜底
    code = data.get("code")
    if code != 200:
        raise CommentsError(
            f"网易云评论接口返回 code={code}：{data.get('message') or data.get('msg') or '未知错误'}"
            "（接口变更或被风控，请稍后重试）"
        )

    comments: list[dict[str, Any]] = []
    seen: set[Any] = set()
    # 置顶 + 热度榜 + 最新页合并去重（commentId 唯一），保证高赞不遗漏
    for c in (data.get("topComments") or []) + (data.get("hotComments") or []) + (data.get("comments") or []):
        cid = c.get("commentId")
        if cid is not None and cid in seen:
            continue
        seen.add(cid)
        text = (c.get("content") or "").strip()
        if not text:
            continue
        user = c.get("user") or {}
        created = c.get("time")
        comments.append({
            "author": user.get("nickname") or "匿名",
            "text": text,
            "likes": int(c.get("likedCount") or 0),
            # 接口给毫秒时间戳，归一到秒（与 bilibili ctime / generic timestamp 同单位）
            "time": int(created // 1000) if isinstance(created, (int, float)) else None,
        })
    return {"title": _resolve_title(kind, rid), "comments": comments}
