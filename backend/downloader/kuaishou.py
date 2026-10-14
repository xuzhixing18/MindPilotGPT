"""快手专用解析下载模块（不走 yt-dlp）。

背景：yt-dlp **没有 kuaishou 提取器**（list_extractors 无匹配），快手链接落到
generic 后，yt-dlp 跟进 302 到 www.kuaishou.com/short-video/{id} 便抛生英文
``Unsupported URL``。与视频号不同，快手分享页对匿名服务端**直接内嵌可下载地址**，
故本模块做真正的解析（而非拦截）。

核心原理（2026-10 实测）：
- 分享短链（www.kuaishou.com/f/{token} 或 v.kuaishou.com/{token}）在**移动端 UA**
  下 302 到 ``m.gifshow.com/fw/photo/{photoId}``，该页 HTML 内嵌明文 JSON；
- 视频直链在 ``mainMvUrls[0].url``（含 pkey 授权参数，匿名 Range 请求即 200
  video/mp4，无需 Cookie/登录态）；标题 caption、时长 duration(毫秒)、封面
  coverUrls、作者 user_name、播放量 viewCount 皆在同一段 JSON；
- photoId 从**最终 URL 路径**取（/f/ 短链的 token 不等于 photoId，必须跟随重定向）；
  视频直链的 clientCacheKey 也内嵌 photoId，可佐证取到的是当前视频而非推荐位。

对外暴露与 downloader 同构的 extract_info / download，FastAPI 与前端无需改动即可复用。
解析失败（风控页/无 mainMvUrls）统一抛 KuaishouError（上层按 400 + 中文提示处理）。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from backend.downloader import common

DOWNLOAD_DIR = common.DOWNLOAD_DIR

# 移动端 UA：快手分享页仅在移动端 UA 下 302 到含明文数据的 m.gifshow.com 页
MOBILE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
        "Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://m.gifshow.com/",
}

_TIMEOUT = (10, 30)
_MAX_RETRIES = 3

_session = requests.Session()
_session.headers.update(MOBILE_HEADERS)


class KuaishouError(ValueError):
    """快手解析/下载失败（上层按 400 处理）。"""


# --------------------------------------------------------------------------- #
# 链接识别
# --------------------------------------------------------------------------- #
def can_handle(url: str) -> bool:
    """统一调度契约：识别快手域名（kuaishou.com / gifshow.com 及其子域、快手 CDN 分享）。"""
    host = (urlparse(url).netloc or "").lower()
    # 去端口与 www. 前缀后的裸域判定
    host = host.rsplit(":", 1)[0]
    return (
        host == "kuaishou.com" or host.endswith(".kuaishou.com")
        or host == "gifshow.com" or host.endswith(".gifshow.com")
        or host == "kwai.com" or host.endswith(".kwai.com")
    )


# 保留旧名风格，兼容潜在调用
is_kuaishou_url = can_handle


def extract_first_url(text: str) -> str:
    """从分享文案中提取第一个 http(s) 链接；若本身就是链接则原样返回。"""
    match = re.search(r"https?://[^\s<>\"']+", text)
    return match.group(0) if match else text.strip()


# --------------------------------------------------------------------------- #
# 页面获取与字段解析
# --------------------------------------------------------------------------- #
_PHOTO_ID_RE = re.compile(r"/(?:fw/photo|short-video|photo|show)/([^/?&#]+)")


def _photo_id_from_url(url: str) -> str | None:
    m = _PHOTO_ID_RE.search(url)
    return m.group(1) if m else None


def _fetch_share_page(url: str) -> tuple[str, str, str]:
    """跟随重定向取分享页，返回 (photoId, html, final_url)。

    优先信任移动端 302 终点页里的明文数据；若终点页无数据（偶发风控），
    用 URL 里的 photoId 直接兜底请求 m.gifshow.com/fw/photo/{id} 再试一次。
    """
    try:
        resp = _session.get(url, timeout=_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        html, final_url = resp.text or "", resp.url
    except requests.RequestException as exc:
        raise KuaishouError(f"无法打开快手分享链接：{exc}") from exc

    photo_id = _photo_id_from_url(final_url)
    if "mainMvUrls" not in html and photo_id:
        # 终点页未内嵌数据：用 photoId 兜底再取移动详情页
        try:
            resp = _session.get(f"https://m.gifshow.com/fw/photo/{photo_id}", timeout=_TIMEOUT, allow_redirects=True)
            resp.raise_for_status()
            html, final_url = resp.text or "", resp.url
        except requests.RequestException:
            pass  # 兜底失败沿用原 html，交由下方统一判定
    if "mainMvUrls" not in html:
        raise KuaishouError(
            "未能从快手页面解析到视频数据（可能触发风控或视频已删除），请稍后重试。"
        )
    if not photo_id:
        photo_id = _photo_id_from_url(final_url) or ""
    return photo_id, html, final_url


def _regex_str(html: str, pattern: str) -> str | None:
    m = re.search(pattern, html)
    return m.group(1) if m else None


def _regex_int(html: str, pattern: str) -> int | None:
    m = re.search(pattern, html)
    return int(m.group(1)) if m else None


def _unescape(raw: str | None) -> str:
    """JSON 字符串反转义（caption 里可能含 \\u、\\"、\\n 等）。"""
    if not raw:
        return ""
    try:
        return json.loads(f'"{raw}"')
    except ValueError:
        return raw


# mainMvUrls 首个数组元素里的 url（minified JSON：{"cdn":"...","url":"..."}）
def _video_url(html: str) -> str | None:
    return _regex_str(html, r'"mainMvUrls":\[\{[^{}]*?"url":"([^"]+)"')


def _cover_url(html: str) -> str | None:
    return _regex_str(html, r'"coverUrls":\[\{[^{}]*?"url":"([^"]+)"')


def _parse_fields(photo_id: str, html: str) -> dict[str, Any]:
    caption = _unescape(_regex_str(html, r'"caption":"((?:[^"\\]|\\.)*)"'))
    duration_ms = _regex_int(html, r'"duration":(\d+)')
    height = _regex_int(html, r'"height":(\d+)') or 0
    width = _regex_int(html, r'"width":(\d+)') or 0
    return {
        "photo_id": photo_id,
        "video_url": _video_url(html),
        "title": caption or (f"kuaishou_{photo_id}" if photo_id else "快手视频"),
        "thumbnail": _cover_url(html),
        "duration": (duration_ms / 1000) if duration_ms else None,
        "uploader": _unescape(_regex_str(html, r'"user_name":"((?:[^"\\]|\\.)*)"')),
        "view_count": _regex_int(html, r'"viewCount":(\d+)'),
        "width": width,
        "height": height,
    }


def _resolution_label(height: int) -> str:
    return f"{height}p" if height else "在线清晰度"


def resolve_photo_id(url: str) -> tuple[str, str]:
    """解析分享链接 → (photoId, 视频标题)，供评论等下游模块复用。

    与 extract_info 同走跟随重定向取 photoId 的逻辑（/f/ 短链 token≠photoId）；
    失败抛 KuaishouError（ValueError 子类，上层按 400）。
    """
    photo_id, html, _ = _fetch_share_page(extract_first_url(url))
    f = _parse_fields(photo_id, html)
    return photo_id, f["title"]


# --------------------------------------------------------------------------- #
# 对外接口（与 downloader 同构）
# --------------------------------------------------------------------------- #
def extract_info(url: str) -> dict[str, Any]:
    """解析快手视频信息，返回与 downloader.extract_info 相同的结构。"""
    photo_id, html, final_url = _fetch_share_page(extract_first_url(url))
    f = _parse_fields(photo_id, html)
    if not f["video_url"]:
        raise KuaishouError("未能解析出快手视频地址，请稍后重试。")

    formats = [
        {
            "format_id": "kuaishou_video",
            "ext": "mp4",
            "resolution": _resolution_label(f["height"]),
            "height": f["height"],
            "filesize": None,
            "has_audio": True,
            "progressive": True,
            "label": f"{_resolution_label(f['height'])} 视频 MP4",
        }
    ]

    return {
        "id": f["photo_id"],
        "title": f["title"],
        "thumbnail": f["thumbnail"],
        "duration": f["duration"],
        "uploader": f["uploader"] or None,
        "webpage_url": final_url,
        "extractor": "Kuaishou",
        "view_count": f["view_count"],
        "description": f["title"],
        "ffmpeg_available": True,  # 快手为单文件含声视频，无需 ffmpeg 合并
        "formats": formats,
    }


def _stream_download(media_url: str, target: Path) -> None:
    last_error: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            with _session.get(media_url, stream=True, timeout=_TIMEOUT, allow_redirects=True) as resp:
                resp.raise_for_status()
                with target.open("wb") as fh:
                    for chunk in resp.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            fh.write(chunk)
            return
        except (requests.RequestException, OSError) as exc:
            last_error = exc
            target.unlink(missing_ok=True)
            if attempt == _MAX_RETRIES:
                break
            time.sleep(1.0 * (2 ** (attempt - 1)))
    raise KuaishouError(f"下载快手媒体失败：{last_error}")


def download(url: str, format_id: str | None = None) -> dict[str, Any]:
    """下载快手视频到临时目录，返回与 downloader.download 相同的结构。"""
    photo_id, html, _ = _fetch_share_page(extract_first_url(url))
    f = _parse_fields(photo_id, html)
    media_url = f["video_url"]
    if not media_url:
        raise KuaishouError("未能解析出快手视频地址，无法下载。")

    token = uuid.uuid4().hex[:12]
    target = DOWNLOAD_DIR / f"{token}.mp4"
    _stream_download(media_url, target)

    if not target.exists() or target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        raise KuaishouError("下载结果为空，可能被平台限制。")

    return {
        "filepath": str(target),
        "filename": f"{common.sanitize_filename(f['title'], 'kuaishou_video')}.mp4",
        "filesize": target.stat().st_size,
        "title": f["title"],
    }
