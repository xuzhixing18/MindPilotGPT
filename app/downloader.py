"""yt-dlp 封装层。

设计原则（遵循需求「站在巨人肩膀上，直接封装，尽量减少代码改动」）：
- 不修改 yt-dlp 源码，仅通过其官方 Python API (YoutubeDL) 调用；
- 对外暴露两个简单能力：解析视频信息 (extract_info) 与服务端下载 (download)；
- 网页/手机端无法直接访问服务器文件系统，因此下载采用「服务端下载到临时目录
  → 由接口流式回传给浏览器」的方式，用后即清理。
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, sanitize_filename

# 下载临时目录：所有服务端下载文件先落到这里，回传完成后清理
DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "mindpilot_downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def _ffmpeg_path() -> str | None:
    """探测可用的 ffmpeg 可执行文件路径。

    优先级：系统 PATH 上的 ffmpeg → pip 包 imageio-ffmpeg 内置的二进制。
    后者让项目「零配置、无需管理员权限」即可合并音视频，保持轻量。
    """
    sys_ffmpeg = shutil.which("ffmpeg")
    if sys_ffmpeg:
        return sys_ffmpeg
    try:
        import imageio_ffmpeg

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled and Path(bundled).exists():
            return bundled
    except Exception:  # noqa: BLE001 imageio-ffmpeg 未安装或获取失败时忽略
        pass
    return None


def ffmpeg_available() -> bool:
    """是否具备 ffmpeg 能力（用于合并「纯视频流 + 纯音频流」）。

    B站 / YouTube 等平台高清视频普遍采用音视频分离（DASH），
    需要 ffmpeg 合并；具备后即可解锁完整清晰度选项。
    """
    return _ffmpeg_path() is not None


def _base_opts() -> dict[str, Any]:
    """YoutubeDL 公共参数。"""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,  # 只处理单个视频，不自动展开播放列表
        "socket_timeout": 30,
        "retries": 2,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        },
    }
    ffmpeg = _ffmpeg_path()
    if ffmpeg:
        # 指向探测到的 ffmpeg（含 imageio-ffmpeg 内置二进制）
        opts["ffmpeg_location"] = ffmpeg
    return opts


def _format_resolution(fmt: dict[str, Any]) -> str:
    """把单个 format 归纳成人类可读的清晰度标签，如 1080p / 720p。"""
    height = fmt.get("height")
    if height:
        return f"{height}p"
    res = fmt.get("resolution")
    if res and res != "audio only":
        return res
    if fmt.get("vcodec") in (None, "none"):
        return "audio"
    return "unknown"


def _collect_formats(info: dict[str, Any]) -> list[dict[str, Any]]:
    """从 yt-dlp 的 info 中整理出可选清晰度列表。

    优先返回「含声音的单文件」(progressive)，这类无需 ffmpeg 即可直接下载。
    若系统装有 ffmpeg，则额外返回高清「纯视频流」选项（下载时会自动合并音频）。
    """
    has_ffmpeg = ffmpeg_available()
    seen: dict[tuple[str, str, bool], dict[str, Any]] = {}

    for fmt in info.get("formats") or []:
        vcodec = fmt.get("vcodec")
        acodec = fmt.get("acodec")
        has_video = vcodec not in (None, "none")
        has_audio = acodec not in (None, "none")

        # 跳过纯音频（后面单独处理）与无扩展名的流
        ext = fmt.get("ext")
        if not ext or not has_video:
            continue

        progressive = has_video and has_audio  # 单文件含声音
        # 没有 ffmpeg 时，只保留能直接下载的单文件清晰度
        if not has_ffmpeg and not progressive:
            continue

        resolution = _format_resolution(fmt)
        key = (resolution, ext, progressive)
        # 同一清晰度取体积更大的（通常质量更好）
        candidate = {
            "format_id": fmt.get("format_id"),
            "ext": ext,
            "resolution": resolution,
            "height": fmt.get("height") or 0,
            "filesize": fmt.get("filesize") or fmt.get("filesize_approx"),
            "has_audio": has_audio,
            "progressive": progressive,
            "label": f"{resolution} {ext.upper()}" + ("" if progressive else " (高清·需合并)"),
        }
        prev = seen.get(key)
        if prev is None or (candidate["filesize"] or 0) > (prev["filesize"] or 0):
            seen[key] = candidate

    result = list(seen.values())
    # 高清优先，含声音的单文件优先
    result.sort(key=lambda f: (f["height"], f["progressive"]), reverse=True)
    return result


def _collect_audio_option(info: dict[str, Any]) -> dict[str, Any] | None:
    """整理「仅音频」下载选项（提取 MP3/M4A 等）。"""
    best_audio = None
    for fmt in info.get("formats") or []:
        if fmt.get("vcodec") not in (None, "none"):
            continue
        if best_audio is None:
            best_audio = fmt
        else:
            if (fmt.get("abr") or 0) > (best_audio.get("abr") or 0):
                best_audio = fmt
    if not best_audio:
        return None
    return {
        "format_id": best_audio.get("format_id"),
        "ext": best_audio.get("ext") or "m4a",
        "resolution": "audio",
        "height": 0,
        "filesize": best_audio.get("filesize") or best_audio.get("filesize_approx"),
        "has_audio": True,
        "progressive": True,
        "is_audio_only": True,
        "label": f"仅音频 {best_audio.get('ext', 'audio').upper()}",
    }


def extract_info(url: str) -> dict[str, Any]:
    """解析视频链接，返回标题、封面、时长与可选清晰度列表。

    对应接口 GET /api/info。此步骤不下载文件 (download=False)。
    """
    opts = _base_opts()
    opts["skip_download"] = True

    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as exc:
        raise ValueError(f"无法解析该链接：{exc}") from exc

    formats = _collect_formats(info)
    audio = _collect_audio_option(info)
    if audio:
        formats.append(audio)

    return {
        "id": info.get("id"),
        "title": info.get("title") or "未知标题",
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader") or info.get("channel"),
        "webpage_url": info.get("webpage_url") or url,
        "extractor": info.get("extractor_key") or info.get("extractor"),
        "ffmpeg_available": ffmpeg_available(),
        "formats": formats,
    }


def download(url: str, format_id: str | None = None) -> dict[str, Any]:
    """服务端下载视频到临时目录，返回文件路径等信息。

    对应接口 POST /api/download 的核心逻辑。下载完成后由调用方
    （FastAPI）负责把文件流式回传给浏览器，并在回传后清理临时文件。

    :param url: 视频链接
    :param format_id: 指定清晰度 format_id；为空则自动选择最佳可下载清晰度
    """
    token = uuid.uuid4().hex[:12]
    outtmpl = str(DOWNLOAD_DIR / f"{token}.%(ext)s")

    # 清晰度选择策略：
    # - 指定 format_id：视频类自动附带最佳音频（有 ffmpeg 时合并）
    # - 未指定：有 ffmpeg 时优先最佳画质合并，否则回退到单文件最佳
    has_ffmpeg = ffmpeg_available()
    if format_id:
        if has_ffmpeg:
            fmt = f"{format_id}+bestaudio/{format_id}/best[ext=mp4]/best"
        else:
            fmt = f"{format_id}/best[ext=mp4]/best"
    else:
        if has_ffmpeg:
            fmt = "bestvideo+bestaudio/best[ext=mp4]/best"
        else:
            fmt = "best[ext=mp4]/best"

    opts = _base_opts()
    opts.update(
        {
            "format": fmt,
            "outtmpl": outtmpl,
            "noprogress": True,
        }
    )
    if has_ffmpeg:
        # 合并后统一输出 mp4，兼容性最好
        opts["merge_output_format"] = "mp4"

    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except DownloadError as exc:
        raise ValueError(f"下载失败：{exc}") from exc

    # 现代 yt-dlp 会把最终文件路径写入 requested_downloads
    filepath = None
    for req in info.get("requested_downloads") or []:
        if req.get("filepath"):
            filepath = Path(req["filepath"])
            break
    if filepath is None:
        raise ValueError("下载完成但未定位到文件，请重试或更换清晰度。")

    if not filepath.exists():
        raise ValueError("下载的文件不存在，可能被平台限制。")

    safe_title = sanitize_filename(info.get("title") or "video")
    return {
        "filepath": str(filepath),
        "filename": f"{safe_title}{filepath.suffix}",
        "filesize": filepath.stat().st_size,
        "title": info.get("title"),
    }
