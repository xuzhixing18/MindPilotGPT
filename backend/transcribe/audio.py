"""音频获取（供 ASR 兜底）—— 把视频 URL 变成一个可上传的音频文件。

字幕抓不到时，门面会调用本模块拿音频再交给 ASR。路由与下载层一致：
- 抖音：走 backend.downloader.douyin 下载无水印视频（含音频轨），再用 ffmpeg 抽音频；
- 其他平台：走 yt-dlp 抓 bestaudio（按需注入 Cookie）。

统一转成 16kHz 单声道 mp3：体积小（满足 DashScope base64 ≤10MB）、格式通用、
对语音识别足够；无 ffmpeg 时原样返回下载到的音频容器（SenseVoiceSmall 亦接受 m4a 等）。
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from typing import Any

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from backend.downloader import common, douyin
from backend.transcribe import subtitles  # 复用其 _apply_cookies（同为 yt-dlp opts 注入）

# 与下载/字幕共用同一临时目录（音频用完即清）
DOWNLOAD_DIR = common.DOWNLOAD_DIR

_FFMPEG_TIMEOUT = 300  # 音频转码超时（秒）


class AudioError(ValueError):
    """音频获取/转码失败（上层按 400 处理）。"""


def acquire_audio(url: str) -> tuple[Path, str]:
    """获取 URL 对应的音频文件。

    :return: (音频文件路径, 标题)。调用方负责用后删除该文件。
    :raises AudioError: 下载或转码失败
    """
    if douyin.can_handle(url):
        return _douyin_audio(url)
    return _ytdlp_audio(url)


def _to_mono_mp3(src: Path) -> Path:
    """用 ffmpeg 抽取/转码为 16kHz 单声道 mp3；无 ffmpeg 或失败时原样返回 src。"""
    ffmpeg = common.ffmpeg_path()
    if not ffmpeg or not src.is_file():
        return src
    dst = src.parent / f"{src.stem}_asr.mp3"
    cmd = [
        ffmpeg, "-y", "-i", str(src),
        "-vn",              # 去视频流，只留音频
        "-ac", "1",         # 单声道
        "-ar", "16000",     # 16kHz 采样（ASR 标准）
        "-b:a", "48k",      # 48kbps，语音足够且体积小
        str(dst),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=_FFMPEG_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return src
    if proc.returncode == 0 and dst.is_file() and dst.stat().st_size > 0:
        src.unlink(missing_ok=True)  # 转码成功后删除源文件（mp4/m4a）
        return dst
    dst.unlink(missing_ok=True)
    return src


def _douyin_audio(url: str) -> tuple[Path, str]:
    """抖音：下载无水印视频（含音频轨）→ ffmpeg 抽音频。"""
    try:
        res = douyin.download(url)  # format_id 为空 -> 无水印 mp4
    except douyin.DouyinError as exc:
        raise AudioError(f"下载抖音音频失败：{exc}") from exc
    src = Path(res["filepath"])
    if not src.is_file():
        raise AudioError("抖音视频下载结果为空，无法提取音频。")
    title = res.get("title") or "抖音视频"
    return _to_mono_mp3(src), title


def _ytdlp_audio(url: str) -> tuple[Path, str]:
    """其他平台：yt-dlp 抓 bestaudio（按需注入 Cookie）→ ffmpeg 转码。"""
    token = uuid.uuid4().hex[:12]
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "outtmpl": str(DOWNLOAD_DIR / f"{token}.%(ext)s"),
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
    ffmpeg = common.ffmpeg_path()
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
    subtitles._apply_cookies(opts)  # 复用字幕模块的 Cookie 注入逻辑

    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except DownloadError as exc:
        raise AudioError(f"下载音频失败：{exc}") from exc

    filepath: Path | None = None
    for req in info.get("requested_downloads") or []:
        if req.get("filepath"):
            filepath = Path(req["filepath"])
            break
    if filepath is None or not filepath.is_file():
        raise AudioError("音频下载完成但未定位到文件，请重试。")

    title = info.get("title") or "未知标题"
    return _to_mono_mp3(filepath), title
