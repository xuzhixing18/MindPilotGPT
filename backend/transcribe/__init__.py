"""内容转写包：把视频/音频转成文字。

门面 ``transcribe(url)`` 编排「字幕优先 → ASR 兜底」两级策略：
1. 先用 yt-dlp 抓视频自带的人工/自动字幕（免费、快、带时间戳）；
2. 抓不到字幕时（抖音无字幕轨、B站未登录等），若配置了 ASR，则下载音频调
   第三方语音识别兜底；未配置 ASR 时给出差异化、可操作的提示（区分平台）。

调用方（main.py / ai 总结）只依赖门面 ``transcribe`` / ``TranscribeError``，
无需感知底层是字幕还是语音识别。扩展约定（延续「门面 + 可插拔」风格）：
- 新增字幕来源：改 subtitles.py；
- 新增 ASR 服务商：在 asr.py 的 _PROVIDERS 加一行；
- 新增平台音频获取：在 audio.py 里按 can_handle 分支。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# 导入顺序：subtitles（无兄弟依赖）→ asr（无兄弟依赖）→ audio（依赖 subtitles）
from backend.transcribe import subtitles, asr, audio
from backend.transcribe.asr import ASRError, ASRNotConfiguredError
from backend.transcribe.subtitles import TranscribeError
from backend import storage  # 内容缓存 + 并发去重（阶段0：SQLite）

__all__ = [
    "transcribe",
    "TranscribeError",
    "ASRError",
    "ASRNotConfiguredError",
    "asr_available",
    "asr_label",
]


def asr_available() -> bool:
    """是否已配置可用的语音识别（ASR）兜底。"""
    return asr.asr_available()


def asr_label() -> str:
    """当前首选 ASR 服务商展示名（未配置时返回空串）。"""
    return asr.asr_label()


def _host(url: str) -> str:
    return (urlparse(url).netloc or "").lower()


def _no_subtitle_hint(url: str, sub_error: Exception) -> str:
    """未配置 ASR 时，按平台给出可操作提示（区分抖音 / B站 / 其他）。"""
    host = _host(url)
    if "douyin.com" in host or "iesdouyin.com" in host:
        return (
            "抖音视频没有独立字幕轨，无法直接提取字幕。可配置语音识别(ASR)兜底："
            "在 .env 设置 SILICONFLOW_API_KEY（硅基流动 SenseVoice 免费不限量）后重启服务即可转写。"
        )
    if "bilibili.com" in host or "b23.tv" in host:
        return (
            "该 B站视频未找到可抓字幕。B站 AI 字幕需登录态：请在 .env 配置 "
            "COOKIES_FROM_BROWSER=edge（或 COOKIE_FILE 指向 cookies.txt）后重启；"
            "或设置 SILICONFLOW_API_KEY 启用语音识别兜底。"
        )
    return (
        f"{sub_error} 可在 .env 设置 SILICONFLOW_API_KEY（或 DASHSCOPE_API_KEY）"
        "启用语音识别(ASR)兜底后重试。"
    )


def _transcribe_via_asr(url: str) -> dict[str, Any]:
    """下载音频 → 语音识别 → 组装成与字幕转写同构的结果（source=asr）。"""
    audio_path, title = audio.acquire_audio(url)
    try:
        result = asr.transcribe_audio(audio_path)
    finally:
        Path(audio_path).unlink(missing_ok=True)  # 用完即清
    text = result["text"]
    provider = result.get("provider") or "ASR"
    return {
        "title": title,
        "language": result.get("language") or "",
        "language_name": "语音识别",
        "source": "asr",
        "segments": result.get("segments") or [],
        "text": text,
        "char_count": len(text),
        "webpage_url": url,
        "asr_provider": provider,
    }


def _compute_transcribe(url: str) -> dict[str, Any]:
    """真正执行转写：字幕优先，无字幕则 ASR 兜底（不含缓存逻辑）。"""
    try:
        return subtitles.transcribe(url)  # 第一级：抓视频自带字幕
    except TranscribeError as sub_error:
        # 第二级：语音识别兜底（仅在已配置 ASR 时尝试）
        if not asr.asr_available():
            raise TranscribeError(_no_subtitle_hint(url, sub_error)) from sub_error
        try:
            return _transcribe_via_asr(url)
        except (ASRError, audio.AudioError) as asr_error:
            raise TranscribeError(
                f"未找到字幕，且语音识别兜底失败：{asr_error}"
            ) from asr_error


def transcribe(url: str, *, refresh: bool = False) -> dict[str, Any]:
    """把视频转写为带分段的文本：缓存优先 → 字幕 → ASR 兜底。

    命中缓存直接返回（``cached=True``）；未命中则计算并落库。相同 URL 的并发请求
    经 single-flight 串行化，只计算一次（其余等待后命中缓存）。

    :param url: 视频链接
    :param refresh: 为 True 时跳过缓存、强制重算并覆盖
    :raises TranscribeError: 字幕与 ASR 均不可用 / 均失败
    :return: {title, language, source, segments, text, char_count, cached, ...}，
             source ∈ {manual, auto, asr}
    """
    key = storage.transcript_key(url)
    if not refresh:
        hit = storage.repo.get_transcript(key)
        if hit is not None:
            hit["cached"] = True
            return hit

    def _do() -> dict[str, Any]:
        # 双重检查：拿到 single-flight 锁后再查一次，避免并发重复计算
        if not refresh:
            hit = storage.repo.get_transcript(key)
            if hit is not None:
                hit["cached"] = True
                return hit
        result = _compute_transcribe(url)
        result["cached"] = False
        storage.repo.put_transcript(key, result, url, storage.normalize_url(url))
        return result

    return storage.transcribe_flight.run(key, _do)
