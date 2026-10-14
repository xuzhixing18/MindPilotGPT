"""快手评论抓取：分享页 → photoId → www.kuaishou.com/graphql ``teslaCommentList``（匿名）。

快手**无 yt-dlp 提取器**（generic 兜底 getcomments 永远失败），其 Web 端评论走 GraphQL：

1) 复用 ``downloader.kuaishou.resolve_photo_id`` 跟随分享短链重定向拿到 photoId 与标题
   （/f/ 短链 token≠photoId，必须跟随重定向，逻辑与下载层一致）；
2) 先用 PC UA 访问 ``www.kuaishou.com`` 首页匿名换取 ``did`` 等 Cookie（**无需登录态**，
   首页 200 即下发）；
3) POST ``https://www.kuaishou.com/graphql``，operationName=commentList，查询字段用
   ``teslaCommentList``（普通 ``commentList`` 在该 schema 的 Query 上不存在，会被打回
   GRAPHQL_VALIDATION_FAILED），变量只带 ``photoId`` + ``pcursor``（多传 ``page`` 会报
   Unknown argument）。

返回顺序**并非按点赞排序**（实测 1582/408/23/82… 混序），由门面 ``top_comments`` 重排取
TopN；本模块按 ``pcursor`` 翻页累积到足量、遇 noMore/空游标或达最大页数即停，dedup 以
commentId 兜重复。字段归一：``authorName/content/realLikedCount/timestamp``(毫秒)
→ ``author/text/likes/time``(秒)。

如实上报：HTTP 非 200 / 响应含 GraphQL ``errors`` / 非 JSON（多为风控）→ 抛
``CommentsError``（上层 502，可重试）；只有取到 code 正常且确无评论才返回空列表，交门面判
``CommentsNotSupportedError``（与 bilibili/ximalaya 同一红线：不得把风控伪装成「不支持」）。
"""

from __future__ import annotations

import math
from typing import Any

import requests

from backend.comments.errors import CommentsError
from backend.downloader import kuaishou as _ks

_GRAPHQL = "https://www.kuaishou.com/graphql"
_HOME = "https://www.kuaishou.com/"

# PC UA：GraphQL 是 Web 端接口，用移动端 UA 拿不到 did cookie 上下文
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": _HOME,
    "Content-Type": "application/json",
}
_TIMEOUT = 20

# 单页约 20~32 条；按 limit 估算页数并封顶，避免高 limit 时无限翻页
_EST_PAGE_SIZE = 20
_MAX_PAGES = 8

_QUERY = """query commentList($photoId: String, $pcursor: String) {
  teslaCommentList(photoId: $photoId, pcursor: $pcursor) {
    commentCount
    rootComments {
      commentId
      authorName
      content
      timestamp
      likedCount
      realLikedCount
    }
    pcursor
  }
}"""


def can_handle(url: str) -> bool:
    return _ks.can_handle(url)


def _max_pages(limit: int) -> int:
    return max(1, min(_MAX_PAGES, math.ceil(max(int(limit), 1) / _EST_PAGE_SIZE)))


def _new_session() -> requests.Session:
    """建 PC-UA 会话并访问首页匿名换取 did 等 Cookie（测试可 monkeypatch 本函数跳过网络）。"""
    session = requests.Session()
    session.headers.update(_HEADERS)
    try:
        session.get(_HOME, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise CommentsError(f"快手首页预热失败（未取到匿名 Cookie）：{exc}") from exc
    return session


def _fetch_page(session: requests.Session, photo_id: str, pcursor: str) -> dict[str, Any]:
    """请求一页 teslaCommentList，返回其 data 节点（测试可 monkeypatch 本函数隔离网络）。"""
    payload = {
        "operationName": "commentList",
        "variables": {"photoId": photo_id, "pcursor": pcursor},
        "query": _QUERY,
    }
    try:
        resp = session.post(_GRAPHQL, json=payload, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise CommentsError(f"快手评论接口请求失败：{exc}") from exc
    if resp.status_code != 200:
        raise CommentsError(f"快手评论接口返回 HTTP {resp.status_code}（可能被风控）。")
    try:
        data = resp.json()
    except ValueError as exc:  # 非 JSON 多为风控拦截页
        raise CommentsError("快手评论接口响应非 JSON（可能被风控拦截），请稍后重试。") from exc

    # GraphQL 层错误（schema 变更 / 参数非法 / 服务端拒绝）如实上报，不伪装成「不支持」
    if isinstance(data, dict) and data.get("errors"):
        msg = (data["errors"][0] or {}).get("message") if data["errors"] else ""
        raise CommentsError(f"快手评论接口返回错误：{msg or '未知 GraphQL 错误'}。")
    return (data.get("data") or {}).get("teslaCommentList") or {}


def fetch(url: str, limit: int) -> dict[str, Any]:
    """抓取评论并归一化为 {title, comments:[{author,text,likes,time}]}。"""
    try:
        photo_id, title = _ks.resolve_photo_id(url)
    except _ks.KuaishouError as exc:
        raise CommentsError(f"快手表识解析失败：{exc}") from exc
    if not photo_id:
        raise CommentsError("无法从快手链接识别视频 ID（photoId）。")

    session = _new_session()
    comments: list[dict[str, Any]] = []
    seen: set[Any] = set()
    pcursor = ""
    for _ in range(_max_pages(limit)):
        node = _fetch_page(session, photo_id, pcursor)
        batch = node.get("rootComments") or []
        before = len(seen)
        for c in batch:
            cid = c.get("commentId")
            if cid is not None and cid in seen:
                continue
            seen.add(cid)
            text = (c.get("content") or "").strip()
            if not text:
                continue
            ts = c.get("timestamp")
            comments.append({
                "author": c.get("authorName") or "匿名",
                "text": text,
                "likes": int(c.get("realLikedCount") or c.get("likedCount") or 0),
                # 接口给毫秒时间戳，归一到秒（与 bilibili/ximalaya 同单位）
                "time": int(ts // 1000) if isinstance(ts, (int, float)) else None,
            })
        nxt = node.get("pcursor") or ""
        if len(seen) >= limit or not nxt or nxt == "noMore" or len(seen) == before:
            break  # 足量 / 尾页 / 整页无新条目：停止翻页
        pcursor = nxt
    return {"title": title, "comments": comments}
