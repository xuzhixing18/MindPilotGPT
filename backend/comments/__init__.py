"""高赞评论抓取包 —— 门面 + 可插拔平台解析器（**全程无大模型调用**）。

评论是平台提供的结构化数据（作者/文本/点赞数/时间）：抓取走 yt-dlp 或平台
web 接口，「按赞排序取 TopN」是确定性计算——都不需要 LLM。大模型只在后续可选的
「评论洞察」增强里才会引入（本 MVP 不含）。

延续项目「门面 + 可插拔」风格：新增平台只需新建模块（暴露 ``can_handle`` /
``fetch``）并在 ``_PLATFORMS`` 注册（越靠前优先级越高），未命中回退 ``generic``
（yt-dlp getcomments）。契约与 downloader 包一致，返回结构统一为
``{title, comments:[{author, text, likes, time}]}``。

缓存：评论有时效性，落库带 TTL（``COMMENTS_CACHE_HOURS``，默认 12 小时），区别于
总结/思维导图的无 TTL；并发相同 URL 经 single-flight 串行化，避免重复抓取。
"""

from __future__ import annotations

from typing import Any

from backend import storage
from backend.comments import bilibili, douyin, generic
from backend.comments.errors import CommentsError, CommentsNotSupportedError

DEFAULT_LIMIT = 20

# 专用平台在前（越靠前优先级越高），generic 兜底
_PLATFORMS = [bilibili, douyin]


def _pick(url: str):
    """按序匹配可处理该平台评论的解析器，未命中回退 generic。"""
    for mod in _PLATFORMS:
        if mod.can_handle(url):
            return mod
    return generic


def top_comments(comments: list[dict[str, Any]] | None, limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
    """过滤空评论 → 按点赞数降序 → 取前 limit 条（纯计算，稳定排序）。"""
    clean = [c for c in (comments or []) if (c.get("text") or "").strip()]
    clean.sort(key=lambda c: int(c.get("likes") or 0), reverse=True)
    return clean[: max(int(limit), 0)]


def fetch_comments(url: str, *, refresh: bool = False, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """抓取视频高赞评论（缓存优先 + single-flight）。

    返回 ``{title, source, total, comments, cached}``；平台不支持或无评论时抛
    ``CommentsNotSupportedError``，网络/解析失败抛 ``CommentsError``。
    """
    key = storage.comments_key(url)

    if not refresh:
        hit = storage.repo.get_comments(key)
        if hit is not None:
            hit["cached"] = True
            return hit

    def _do() -> dict[str, Any]:
        # 双重检查：拿到 single-flight 槽后再查一次缓存，避免并发重复抓取
        if not refresh:
            hit = storage.repo.get_comments(key)
            if hit is not None:
                hit["cached"] = True
                return hit

        mod = _pick(url)
        try:
            raw = mod.fetch(url, limit)
        except CommentsError:
            raise
        except Exception as exc:  # noqa: BLE001 兜底，统一转 CommentsError
            raise CommentsError(f"评论抓取失败：{exc}") from exc

        comments = top_comments(raw.get("comments"), limit)
        if not comments:
            raise CommentsNotSupportedError("该平台暂不支持评论抓取，或该视频暂无评论。")

        result = {
            "title": raw.get("title") or "",
            "source": mod.__name__.rsplit(".", 1)[-1],
            "total": len(comments),
            "comments": comments,
            "cached": False,
        }
        storage.repo.put_comments(key, result, url, storage.normalize_url(url))
        return result

    return storage.comments_flight.run(key, _do)


__all__ = [
    "fetch_comments",
    "top_comments",
    "CommentsError",
    "CommentsNotSupportedError",
    "DEFAULT_LIMIT",
]
