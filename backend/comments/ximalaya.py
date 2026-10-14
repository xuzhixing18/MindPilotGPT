"""喜马拉雅评论抓取：sound/album → albumId → ``web-site/albumComment/list``（专辑级评论，无签名）。

喜马拉雅评论不在 yt-dlp 支持范围内（ximalaya extractor 未实现 getcomments，
generic 兜底永远拿到空列表），且 2026 年起评论系统已迁移为**专辑级**
（按 albumId 组织，声音页前端实际调用 ``/web-site/albumComment/list``）：

1) 链接是 ``/sound/{trackId}`` 时，先经 ``mobile.ximalaya.com/v1/track/baseInfo``
   （无鉴权、无 xm-sign）换取 albumId 与声音标题；``/album/{albumId}`` 直达；
2) ``www.ximalaya.com/web-site/albumComment/list?albumId=&order=1&pageSize=20&pageId=``
   取评论：``order=1`` 服务端即按点赞降序；**无需任何签名/Cookie**（mobile 系
   ``comment/track/list`` 才有 xm-sign 风控，别走那条）。

注意：该 web-site 接口实测**忽略 pageId**（第 1/2/3 页返回逐字节相同的 top 20 热度榜），
所以高赞上限就是 20 条；代码仍按 limit 尝试翻页并在整页无新条目时立即停止，
作为「接口将来支持翻页」的前向兼容（dedup 同时兜住 pageId 语义异常导致的重复）。
接口 ``code != 0`` 时抛 ``CommentsError`` 如实上报，只有 code=0 且确无评论才返回
空列表交给门面判 NotSupported（与 bilibili 同一红线：不得伪装「不支持」）。
字段归一：``nickname/content/likes/createdAt``(毫秒) → ``author/text/likes/time``(秒)。
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
    "Referer": "https://www.ximalaya.com/",
    "Accept": "application/json, text/plain, */*",
}
_TIMEOUT = 20

# web-site 接口 pageSize 实测上限 20，且 pageId 被服务端忽略（永远返回 top 20 热度榜）
_PAGE_SIZE = 20
_MAX_PAGES = 5


def _max_requests(limit: int) -> int:
    """本轮允许的最大页请求数（整页重复时 fetch 内会提前 break，不会白翻）。"""
    return max(1, min(_MAX_PAGES, (max(int(limit), 1) + _PAGE_SIZE - 1) // _PAGE_SIZE))


def can_handle(url: str) -> bool:
    return "ximalaya.com" in (url or "").lower()


def _get_json(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """统一 GET + JSON 解析（测试可 monkeypatch 本函数隔离网络）。"""
    try:
        resp = requests.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json() or {}
    except requests.RequestException as exc:
        raise CommentsError(f"喜马拉雅接口请求失败：{exc}") from exc
    except ValueError as exc:  # 非 JSON 响应：多为风控拦截页
        raise CommentsError(f"喜马拉雅接口响应非 JSON（可能被风控拦截）：{exc}") from exc


def _extract_ids(url: str) -> tuple[int | None, int | None]:
    """从链接提取 (trackId, albumId)；sound 页给 trackId，album 页给 albumId。"""
    t = re.search(r"/sound/(\d+)", url)
    a = re.search(r"/album/(\d+)", url)
    track_id = int(t.group(1)) if t else None
    album_id = int(a.group(1)) if a else None
    if track_id is None and album_id is None:
        raise CommentsError("无法从链接中识别喜马拉雅声音（/sound/）或专辑（/album/）ID。")
    return track_id, album_id


def _resolve_album(track_id: int | None, album_id: int | None) -> tuple[int, str]:
    """换取 (albumId, 标题)：sound 链接经 baseInfo 映射，album 链接直接用 albumId。"""
    if album_id is not None:
        return album_id, ""
    data = _get_json(
        "https://mobile.ximalaya.com/v1/track/baseInfo",
        {"device": "android", "trackId": track_id},
    )
    # baseInfo 无 ret 包壳，成功时直接平铺字段；albumId 缺失即视为异常响应
    aid = data.get("albumId")
    if not aid:
        msg = data.get("msg") or ""
        raise CommentsError(f"未能获取该声音所属专辑 ID{'：' + msg if msg else ''}。")
    return int(aid), data.get("title") or ""


def fetch(url: str, limit: int) -> dict[str, Any]:
    """抓取评论并归一化为 {title, comments:[{author,text,likes,time}]}。"""
    track_id, album_id = _extract_ids(url)
    aid, title = _resolve_album(track_id, album_id)

    comments: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for page_id in range(1, _max_requests(limit) + 1):
        data = _get_json(
            "https://www.ximalaya.com/web-site/albumComment/list",
            {"albumId": aid, "order": 1, "pageId": page_id, "pageSize": _PAGE_SIZE},
        )
        # 注意 code=0 是成功值，不能用 `or` 兜底（0 为 falsy 会被误判成异常）
        code = data.get("code")
        if code != 0:
            raise CommentsError(
                f"喜马拉雅评论接口返回 code={code}：{data.get('msg') or '未知错误'}"
                "（接口变更或被风控，请稍后重试）"
            )
        batch = ((data.get("data") or {}).get("comments")) or []
        before = len(seen)
        for c in batch:
            cid = c.get("commentId")
            if cid is not None and cid in seen:
                continue  # pageId 被忽略/语义异常：重复条目直接丢弃
            seen.add(cid)
            text = (c.get("content") or "").strip()
            if not text:
                continue
            created = c.get("createdAt")
            comments.append({
                "author": c.get("nickname") or "匿名",
                "text": text,
                "likes": int(c.get("likes") or 0),
                # 接口给毫秒时间戳，归一到秒（与 bilibili ctime / generic timestamp 同单位）
                "time": int(created // 1000) if isinstance(created, (int, float)) else None,
            })
        if len(seen) == before:
            break  # 整页无新条目：服务端忽略 pageId（或已到尾页），停止无效翻页
    return {"title": title, "comments": comments}
