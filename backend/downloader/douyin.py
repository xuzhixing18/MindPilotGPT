"""抖音专用解析下载模块（不走 yt-dlp）。

背景：yt-dlp 对抖音的支持经常失效，因此对抖音链接单独走本模块，
其他平台仍走通用解析器 generic（由包门面 __init__.py + registry 统一调度）。

核心原理（参考开源实现 rathodpratham-dev/douyin_video_downloader，MIT）：
- 抖音分享短链 (v.douyin.com) 经 302 重定向到含 video_id 的页面；
- 调用 iesdouyin 公开 iteminfo API 获取视频元数据（无需登录态）；
- 播放地址中的 ``playwm``（带水印）替换为 ``play`` 即得无水印视频；
- 公开 API 不可用时，回退到解析分享页内嵌的 ``window._ROUTER_DATA``。

对外暴露与 downloader 同构的两个能力：extract_info / download，
使 FastAPI 接口与前端无需任何改动即可复用。
"""

from __future__ import annotations

import base64
import json
import re
import time
import uuid
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from backend.downloader import common

# 与通用解析器共用同一临时目录（回传后由接口清理）
DOWNLOAD_DIR = common.DOWNLOAD_DIR

API_URL = "https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/"

# 移动端 UA：抖音分享页/重定向对移动端 UA 更友好
MOBILE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
        "Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.douyin.com/",
}

_TIMEOUT = (10, 30)
_MAX_RETRIES = 3

_session = requests.Session()
_session.headers.update(MOBILE_HEADERS)


class DouyinError(ValueError):
    """抖音解析/下载失败（上层按 400 处理）。"""


# --------------------------------------------------------------------------- #
# 链接识别与 video_id 提取
# --------------------------------------------------------------------------- #
def can_handle(url: str) -> bool:
    """统一调度契约：是否由抖音解析器处理（douyin.com / iesdouyin.com 及其子域）。"""
    host = (urlparse(url).netloc or "").lower()
    return host == "douyin.com" or host.endswith(".douyin.com") or host.endswith("iesdouyin.com")


# 保留旧名，兼容既有测试与历史调用
is_douyin_url = can_handle


def extract_first_url(text: str) -> str:
    """从分享文案中提取第一个 http(s) 链接；若本身就是链接则原样返回。"""
    match = re.search(r"https?://[^\s<>\"']+", text)
    return match.group(0) if match else text.strip()


def resolve_video_id(url: str) -> tuple[str, str]:
    """跟随重定向解析出 video_id 与最终 URL。"""
    try:
        resp = _session.get(url, timeout=_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        final_url = resp.url
    except requests.RequestException as exc:
        raise DouyinError(f"无法打开抖音分享链接：{exc}") from exc

    for pattern in (r"/video/(\d+)", r"/note/(\d+)", r"modal_id=(\d+)", r"aweme_id=(\d+)"):
        match = re.search(pattern, final_url)
        if match:
            return match.group(1), final_url
    raise DouyinError("未能从抖音链接中解析出视频 ID，请确认链接是否有效。")


# --------------------------------------------------------------------------- #
# 元数据获取：公开 API 优先，失败回退分享页 _ROUTER_DATA
# --------------------------------------------------------------------------- #
def _fetch_item_info(video_id: str, resolved_url: str) -> dict[str, Any]:
    try:
        data = _session.get(API_URL, params={"item_ids": video_id}, timeout=_TIMEOUT).json()
        if data.get("status_code") in (0, None):
            item_list = data.get("item_list") or []
            if item_list:
                return item_list[0]
        raise DouyinError("公开 API 未返回有效数据")
    except (requests.RequestException, ValueError, DouyinError):
        return _fetch_item_info_from_share_page(video_id, resolved_url)


def _fetch_item_info_from_share_page(video_id: str, resolved_url: str) -> dict[str, Any]:
    share_url = resolved_url if "iesdouyin.com" in (urlparse(resolved_url).netloc or "") \
        else f"https://www.iesdouyin.com/share/video/{video_id}/"
    html = _get_share_page_html(share_url)
    router_data = _extract_router_data_json(html)
    if not router_data:
        raise DouyinError("无法从抖音分享页提取视频数据。")
    item = _extract_item_from_router_data(router_data)
    if not item:
        raise DouyinError("分享页中未找到视频元数据。")
    return item


def _get_share_page_html(share_url: str) -> str:
    resp = _session.get(share_url, timeout=_TIMEOUT)
    resp.raise_for_status()
    html = resp.text or ""
    # 抖音可能先返回 JS WAF 挑战页，求解后重试一次
    if _is_waf_challenge(html) and _solve_waf_and_set_cookie(html, share_url):
        resp = _session.get(share_url, timeout=_TIMEOUT)
        resp.raise_for_status()
        html = resp.text or ""
    return html


def _is_waf_challenge(html: str) -> bool:
    return "Please wait..." in html and "wci=" in html and "cs=" in html


def _solve_waf_and_set_cookie(html: str, page_url: str) -> bool:
    match = re.search(r'wci="([^"]+)"\s*,\s*cs="([^"]+)"', html)
    if not match:
        return False
    cookie_name, blob = match.groups()
    try:
        challenge = json.loads(_b64_decode(blob).decode("utf-8"))
        prefix = _b64_decode(challenge["v"]["a"])
        expected = _b64_decode(challenge["v"]["c"]).hex()
    except (KeyError, ValueError, TypeError):
        return False
    solved = None
    for candidate in range(1_000_001):
        if sha256(prefix + str(candidate).encode()).hexdigest() == expected:
            solved = candidate
            break
    if solved is None:
        return False
    challenge["d"] = base64.b64encode(str(solved).encode()).decode()
    cookie_value = base64.b64encode(json.dumps(challenge, separators=(",", ":")).encode()).decode()
    domain = urlparse(page_url).hostname or "www.iesdouyin.com"
    _session.cookies.set(cookie_name, cookie_value, domain=domain, path="/")
    return True


def _b64_decode(value: str) -> bytes:
    normalized = value.replace("-", "+").replace("_", "/")
    normalized += "=" * (-len(normalized) % 4)
    return base64.b64decode(normalized)


def _extract_router_data_json(html: str) -> dict[str, Any]:
    marker = "window._ROUTER_DATA = "
    start = html.find(marker)
    if start < 0:
        return {}
    index = start + len(marker)
    while index < len(html) and html[index].isspace():
        index += 1
    if index >= len(html) or html[index] != "{":
        return {}
    depth, in_str, escaped = 0, False, False
    for cursor in range(index, len(html)):
        ch = html[cursor]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[index:cursor + 1])
                except ValueError:
                    return {}
    return {}


def _extract_item_from_router_data(router_data: dict[str, Any]) -> dict[str, Any]:
    loader = router_data.get("loaderData") or {}
    for node in loader.values():
        if not isinstance(node, dict):
            continue
        video_info_res = node.get("videoInfoRes") or {}
        item_list = video_info_res.get("item_list") or []
        if item_list and isinstance(item_list[0], dict):
            return item_list[0]
    return {}


# --------------------------------------------------------------------------- #
# 媒体地址
# --------------------------------------------------------------------------- #
def _first_url(node: dict[str, Any]) -> str | None:
    urls = (node or {}).get("url_list") or []
    return urls[0] if urls else None


def no_watermark_play_url(item: dict[str, Any]) -> str:
    """无水印播放地址：playwm(带水印) → play。"""
    raw = _first_url((item.get("video") or {}).get("play_addr") or {})
    if not raw:
        raise DouyinError("未找到视频播放地址。")
    return raw.replace("playwm", "play")


def audio_url(item: dict[str, Any]) -> str | None:
    return _first_url((item.get("music") or {}).get("play_url") or {})


def cover_url(item: dict[str, Any]) -> str | None:
    video = item.get("video") or {}
    for key in ("cover", "origin_cover", "dynamic_cover"):
        url = _first_url(video.get(key) or {})
        if url:
            return url
    return None


# --------------------------------------------------------------------------- #
# 对外接口（与 downloader 同构）
# --------------------------------------------------------------------------- #
def extract_info(url: str) -> dict[str, Any]:
    """解析抖音视频信息，返回与 downloader.extract_info 相同的结构。"""
    video_id, resolved = resolve_video_id(extract_first_url(url))
    item = _fetch_item_info(video_id, resolved)

    duration_ms = (item.get("video") or {}).get("duration") or item.get("duration")
    formats: list[dict[str, Any]] = [
        {
            "format_id": "douyin_video",
            "ext": "mp4",
            "resolution": "无水印",
            "height": 0,
            "filesize": None,
            "has_audio": True,
            "progressive": True,
            "label": "无水印视频 MP4",
        }
    ]
    if audio_url(item):
        formats.append(
            {
                "format_id": "douyin_audio",
                "ext": "mp3",
                "resolution": "audio",
                "height": 0,
                "filesize": None,
                "has_audio": True,
                "progressive": True,
                "is_audio_only": True,
                "label": "仅音频 MP3",
            }
        )

    return {
        "id": video_id,
        "title": item.get("desc") or f"douyin_{video_id}",
        "thumbnail": cover_url(item),
        "duration": (duration_ms / 1000) if duration_ms else None,
        "uploader": (item.get("author") or {}).get("nickname"),
        "webpage_url": resolved,
        "extractor": "Douyin",
        "view_count": (item.get("statistics") or {}).get("play_count"),
        "description": item.get("desc"),
        "ffmpeg_available": True,  # 抖音为单文件，无需 ffmpeg 合并
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
    raise DouyinError(f"下载抖音媒体失败：{last_error}")


def download(url: str, format_id: str | None = None) -> dict[str, Any]:
    """下载抖音视频/音频到临时目录，返回与 downloader.download 相同的结构。"""
    video_id, resolved = resolve_video_id(extract_first_url(url))
    item = _fetch_item_info(video_id, resolved)

    if format_id == "douyin_audio":
        media_url = audio_url(item)
        if not media_url:
            raise DouyinError("该视频未提供可下载的音频。")
        ext = ".mp3"
    else:
        media_url = no_watermark_play_url(item)
        ext = ".mp4"

    token = uuid.uuid4().hex[:12]
    target = DOWNLOAD_DIR / f"{token}{ext}"
    _stream_download(media_url, target)

    if not target.exists() or target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        raise DouyinError("下载结果为空，可能被平台限制。")

    title = item.get("desc") or f"douyin_{video_id}"
    return {
        "filepath": str(target),
        "filename": f"{common.sanitize_filename(title, 'douyin_video')}{ext}",
        "filesize": target.stat().st_size,
        "title": title,
    }
