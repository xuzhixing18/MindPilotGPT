"""字幕提取（转写）—— 基于 yt-dlp 抓取视频自带字幕或平台自动字幕。

MVP 仅做「字幕提取」：B站 / YouTube 等大多数平台的视频都带有人工字幕或
平台自动字幕（ASR），yt-dlp 可直接抓取，免费、快速、无需本地模型。
无字幕时返回明确提示（后续可扩展 Whisper 语音转写兜底）。

对外暴露 transcribe(url) -> dict：既供 /api/transcribe 直接返回字幕，
也作为 AI 总结（backend/ai）的文本来源。
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Any

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from backend.downloader import common

# 项目根目录（backend/transcribe/subtitles.py → transcribe → backend → root）
_ROOT = Path(__file__).resolve().parents[2]

# 尽力加载 .env（与 backend.ai.config 一致；未安装 python-dotenv 时静默跳过）
try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")
except ImportError:  # pragma: no cover - python-dotenv 可选
    pass

# 与下载共用临时目录（字幕文件用完即清，不长期存储）
DOWNLOAD_DIR = common.DOWNLOAD_DIR

# 字幕语言优先级：简中 > 繁中 > 英文 > 日文 > 韩文
_LANG_PRIORITY = [
    "zh-Hans", "zh-CN", "zh-Hant", "zh-TW", "zh-HK", "zh",
    "en", "en-US", "en-GB", "en-orig", "ja", "ko",
]
# 向 yt-dlp 请求的字幕语言（通配，尽量覆盖中英与自动字幕）
_LANG_PATTERNS = ["zh.*", "zh-Hans", "zh-CN", "en.*", "ja", "ko"]

_TAG_RE = re.compile(r"<[^>]+>")  # 去除 VTT 行内标签：<c> <00:00:01.000> 等


class TranscribeError(ValueError):
    """转写失败（上层按 400 处理）。"""


def _apply_cookies(opts: dict[str, Any]) -> None:
    """按需注入 Cookie。

    B站等平台的语音字幕（CC / AI 字幕）需登录态才能获取，无 Cookie 时只能拿到弹幕，
    转写会降级为「未找到字幕」。支持两种方式（二选一，前者优先）：
    - COOKIE_FILE         ：cookies.txt（Netscape 格式）文件路径
    - COOKIES_FROM_BROWSER：从浏览器读取，如 chrome / edge / "chrome:Profile 1"
    未配置时不改动 opts，保持无 Cookie 的降级行为。
    """
    cookie_file = (os.getenv("COOKIE_FILE") or "").strip()
    if cookie_file:
        path = Path(cookie_file).expanduser()
        if path.is_file():
            opts["cookiefile"] = str(path)
            return
    browser = (os.getenv("COOKIES_FROM_BROWSER") or "").strip()
    if browser:
        parts = [p.strip() for p in browser.split(":")]
        name = parts[0]
        profile = parts[1] if len(parts) > 1 and parts[1] else None
        # yt-dlp 约定的四元组：(browser, profile, keyring, container)
        opts["cookiesfrombrowser"] = (name, profile, None, None)


def _subtitle_opts(token: str) -> dict[str, Any]:
    """YoutubeDL 抓字幕专用参数：只下字幕、不下视频本体。"""
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,        # 只取字幕，不下视频本体
        "writesubtitles": True,        # 人工字幕
        "writeautomaticsub": True,     # 平台自动字幕（ASR）
        "subtitleslangs": _LANG_PATTERNS,
        "subtitlesformat": "vtt/srt/best",
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
    _apply_cookies(opts)
    return opts


def _parse_ts(value: str) -> float:
    """把 '00:01:02.500' / '01:02,500' / '01:02.500 align:start' 解析成秒。"""
    if not value or not value.strip():
        return 0.0
    token = value.strip().replace(",", ".").split()[0]
    try:
        nums = [float(p) for p in token.split(":")]
    except ValueError:
        return 0.0
    if len(nums) == 3:
        h, m, s = nums
    elif len(nums) == 2:
        h, m, s = 0.0, nums[0], nums[1]
    elif len(nums) == 1:
        h, m, s = 0.0, 0.0, nums[0]
    else:
        return 0.0
    return h * 3600 + m * 60 + s


def _parse_subtitle_file(path: Path) -> list[dict[str, Any]]:
    """解析 VTT/SRT 字幕文件为分段列表 [{start, end, text}]。"""
    raw = path.read_text(encoding="utf-8", errors="ignore").replace("\ufeff", "")
    segments: list[dict[str, Any]] = []
    for block in re.split(r"\n\s*\n", raw):
        lines = block.splitlines()
        time_idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if time_idx is None:
            continue  # 跳过 WEBVTT 头、NOTE、纯序号块
        start_s, _, end_s = lines[time_idx].partition("-->")
        text_parts = []
        for line in lines[time_idx + 1:]:
            cleaned = _TAG_RE.sub("", line).strip()
            if cleaned:
                text_parts.append(cleaned)
        text = " ".join(text_parts).strip()
        if text:
            segments.append({
                "start": round(_parse_ts(start_s), 2),
                "end": round(_parse_ts(end_s), 2),
                "text": text,
            })
    return _dedupe(segments)


def _dedupe(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """去除自动字幕常见的相邻重复行（滚动式 ASR）。"""
    result: list[dict[str, Any]] = []
    prev = ""
    for seg in segments:
        cur = seg["text"].strip()
        if cur and cur != prev:
            result.append(seg)
            prev = cur
    return result


def _pick_language(requested: dict[str, Any]) -> str | None:
    """按优先级从已下载字幕里选一种语言。"""
    langs = list(requested.keys())
    for want in _LANG_PRIORITY:
        if want in langs:
            return want
    for lang in langs:  # 前缀兜底（如 zh-Hans-CN）
        if lang.split("-")[0] in {"zh", "en", "ja", "ko"}:
            return lang
    return langs[0] if langs else None


def _cleanup(requested: dict[str, Any]) -> None:
    """删除下载到临时目录的字幕文件（用完即清）。"""
    for sub in requested.values():
        filepath = (sub or {}).get("filepath")
        if filepath:
            Path(filepath).unlink(missing_ok=True)


def transcribe(url: str) -> dict[str, Any]:
    """抓取视频字幕并解析为带时间戳的分段文本。

    :param url: 视频链接
    :raises TranscribeError: 无字幕或下载/解析失败
    :return: {title, language, source, segments, text, char_count, ...}
    """
    token = uuid.uuid4().hex[:12]
    try:
        with YoutubeDL(_subtitle_opts(token)) as ydl:
            info = ydl.extract_info(url, download=True)
    except DownloadError as exc:
        raise TranscribeError(f"无法获取该链接的字幕：{exc}") from exc

    requested = info.get("requested_subtitles") or {}
    title = info.get("title") or "未知标题"
    if not requested:
        raise TranscribeError("未找到该视频的字幕（人工与自动字幕均无），暂不支持转写。")

    try:
        lang = _pick_language(requested)
        sub = requested.get(lang) or {}
        filepath = sub.get("filepath")
        if not filepath or not Path(filepath).exists():
            raise TranscribeError("字幕文件下载失败，请重试。")
        segments = _parse_subtitle_file(Path(filepath))
    finally:
        _cleanup(requested)  # 无论成功与否都清理临时字幕

    if not segments:
        raise TranscribeError("字幕内容为空，无法转写。")

    auto_captions = info.get("automatic_captions") or {}
    manual_subs = info.get("subtitles") or {}
    is_auto = lang in auto_captions and lang not in manual_subs
    text = " ".join(s["text"] for s in segments).strip()

    return {
        "title": title,
        "language": lang,
        "language_name": sub.get("name") or lang,
        "source": "auto" if is_auto else "manual",
        "segments": segments,
        "text": text,
        "char_count": len(text),
        "webpage_url": info.get("webpage_url") or url,
    }
