"""抖音评论抓取：复用 ``downloader.douyin`` 的视频 ID 解析 + iesdouyin 评论接口（best-effort）。

抖音视频 ID 解析（含短链跳转）已在 downloader.douyin 实现，这里直接复用其
``can_handle`` / ``extract_first_url`` / ``resolve_video_id``，只额外调评论接口。

iesdouyin 评论接口参数为 ``aweme_id``（旧参数 ``item_id`` 已废弃，返回
「参数不合法」）；成功时 ``status_code`` 为 ``{"StatusCode": 0}``，失败时为 int
（5 参数不合法 / -99999 需签名），时间字段为驼峰 ``createTime``（unix 秒）。
故对响应做精确分类：正常 → 归一化返回；真无评论 → NotSupported；签名网关/
风控（5 / -99999 / 非 JSON）→ NotSupported 并给出准确原因，避免误导用户以为
“视频没评论”。网络异常仍抛 CommentsError（可重试）。
"""

from __future__ import annotations

from typing import Any

import requests

from backend.comments.errors import CommentsError, CommentsNotSupportedError
from backend.downloader import douyin as _douyin

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://www.douyin.com/",
}
_TIMEOUT = 20
_COMMENT_API = "https://www.iesdouyin.com/web/api/v2/comment/list/"

# 接口拒绝（status_code 5/-99999）或风控返回非 JSON 时的统一降级提示
_GATED_MSG = "抖音评论接口暂时拒绝请求，请稍后重试或改用 B 站 / YouTube 链接"


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
            _COMMENT_API,
            params={
                "aweme_id": video_id,
                "cursor": 0,
                "count": min(max(int(limit), 1), 50),
            },
            headers=_HEADERS, timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        # 非 JSON / 空响应多为风控拦截，归入签名网关不可用（而非“抓取失败”）
        raise CommentsNotSupportedError(_GATED_MSG) from exc

    if not isinstance(data, dict):
        raise CommentsNotSupportedError(_GATED_MSG)

    status = data.get("status_code")
    raw = data.get("comments")
    # 成功时 status_code 为 {"StatusCode": 0}，失败时为 int（5 参数不合法 / -99999 需签名）
    code = status.get("StatusCode") if isinstance(status, dict) else status
    if code != 0 or raw is None:
        raise CommentsNotSupportedError(_GATED_MSG)
    if not raw:
        raise CommentsNotSupportedError("该视频暂无评论或评论已关闭。")

    comments: list[dict[str, Any]] = []
    for c in raw:
        text = (c.get("text") or "").strip()
        if not text:
            continue
        comments.append({
            "author": ((c.get("user") or {}).get("nickname")) or "匿名",
            "text": text,
            "likes": int(c.get("digg_count") or 0),
            "time": c.get("createTime"),
        })
    if not comments:
        raise CommentsNotSupportedError("该视频暂无可展示的评论。")
    return {"title": "", "comments": comments}
