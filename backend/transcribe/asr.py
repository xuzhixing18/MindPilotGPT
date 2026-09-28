"""语音识别（ASR）兜底 —— provider 链，config-gated。

用途：当视频抓不到字幕时（抖音无字幕轨、B站未登录等），下载音频调用第三方
ASR 转写为文本，作为字幕提取的兜底。与 backend.ai 的多服务商风格一致：

- 主：阿里云百炼 Fun-ASR-Flash —— 原生 HTTP 接口（SSE 流式），返回**句级时间戳**
  （begin_time/end_time 毫秒），是唯一能产出带时间轴字幕的 provider（base64
  Data URI 直传，编码后 ≤ 10MB）；
- 备：硅基流动 SenseVoiceSmall —— OpenAI 兼容 ``/audio/transcriptions``（multipart
  直传文件），完全免费不限量，中文优化，单文件 ≤50MB / ≤1h，但**无时间戳**；
- 备：阿里云百炼 Qwen3-ASR-Flash —— OpenAI 兼容 ``chat.completions`` + ``input_audio``
  （base64 Data URL），同样无时间戳。

按 env 组成「可用 provider 链」并依次尝试直到成功：默认顺序 Fun-ASR 优先（时间戳
对字幕体验关键）；未配 DashScope Key 时自动降级硅基流动；未配置任何 Key 时
``asr_available()`` 为 False，上层降级为「无字幕」提示（不报错）。密钥只从环境
变量 / .env 读取，绝不硬编码。

也支持完全自定义的 OpenAI 兼容 ASR 端点（ASR_API_KEY + ASR_BASE_URL + ASR_MODEL），
便于随时切换到 Groq / OpenAI / 自建服务等。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import subprocess
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from typing import Any

import httpx

from backend.downloader.common import ffmpeg_path as _ffmpeg_path   # 叶子模块：能力探测，无循环依赖

# 项目根目录（backend/transcribe/asr.py → transcribe → backend → root）
_ROOT = Path(__file__).resolve().parents[2]

# 尽力加载 .env（与 backend.ai.config 一致；未安装 python-dotenv 时静默跳过）
try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")
except ImportError:  # pragma: no cover - python-dotenv 可选
    pass


class ASRError(RuntimeError):
    """语音识别失败（网络 / 鉴权 / 限流 / 超限 / 响应格式）。"""


class ASRNotConfiguredError(RuntimeError):
    """未配置任何可用的 ASR 服务商 Key。"""


# 三种调用风格：
#   funasr_sse      —— 百炼原生 HTTP + SSE（X-DashScope-SSE: enable）：句级时间戳（Fun-ASR-Flash）
#   transcriptions  —— OpenAI 标准 /audio/transcriptions（multipart 直传文件）：硅基流动 / Groq / OpenAI
#   chat_audio      —— OpenAI 兼容 chat.completions + input_audio(base64 Data URL)：阿里云百炼 Qwen3-ASR
_PROVIDERS: dict[str, dict[str, Any]] = {
    "dashscope_funasr": {
        "base_url": "https://dashscope.aliyuncs.com",
        "model": "fun-asr-flash-2026-06-15",
        "key_envs": ["DASHSCOPE_API_KEY", "QWEN_API_KEY"],
        "style": "funasr_sse",
        "label": "通义 Fun-ASR（带时间戳）",
        "max_bytes": 50 * 1024 * 1024,   # 长音频会先切片（单次调用限 300s，切片后远小于此）
    },
    "siliconflow": {
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "FunAudioLLM/SenseVoiceSmall",
        "key_envs": ["SILICONFLOW_API_KEY"],
        "style": "transcriptions",
        "label": "硅基流动 SenseVoice",
        "max_bytes": 50 * 1024 * 1024,   # 单文件 ≤50MB
    },
    "dashscope": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen3-asr-flash",
        "key_envs": ["DASHSCOPE_API_KEY", "QWEN_API_KEY"],
        "style": "chat_audio",
        "label": "通义 Qwen3-ASR",
        "max_bytes": 7 * 1024 * 1024,    # base64 膨胀 ~37%，控制在 10MB 以内
    },
}

# provider 别名 -> 规范键（让用户按习惯填写）
_PROVIDER_ALIASES: dict[str, str] = {
    "funasr": "dashscope_funasr", "fun-asr": "dashscope_funasr",
    "fun-asr-flash": "dashscope_funasr", "fun_asr": "dashscope_funasr",
    "sensevoice": "siliconflow", "sf": "siliconflow", "siliconcloud": "siliconflow",
    "qwen": "dashscope", "qwen-asr": "dashscope", "qwen3-asr": "dashscope",
    "tongyi": "dashscope", "aliyun": "dashscope", "bailian": "dashscope",
}

# 默认尝试顺序（主 → 备）：Fun-ASR 优先（唯一带时间戳），无 DashScope Key 时自动跳过
_DEFAULT_ORDER = ["dashscope_funasr", "siliconflow", "dashscope"]


def _env(*names: str) -> str:
    """按顺序返回第一个非空环境变量值（已去除首尾空白）。"""
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return ""


@dataclass(frozen=True)
class ASRConfig:
    """一次 ASR 调用所需的完整配置。"""

    provider: str
    label: str
    base_url: str
    api_key: str
    model: str
    style: str
    max_bytes: int


def _resolve_order() -> list[str]:
    """解析 provider 尝试顺序：ASR_PROVIDER（可逗号分隔显式排序）优先，其余按默认序追加。"""
    raw = _env("ASR_PROVIDER")
    order: list[str] = []
    if raw:
        for part in raw.split(","):
            key = part.strip().lower()
            canon = _PROVIDER_ALIASES.get(key, key)
            if canon in _PROVIDERS and canon not in order:
                order.append(canon)
    for provider in _DEFAULT_ORDER:  # 补齐未显式列出的，保持默认相对顺序
        if provider not in order:
            order.append(provider)
    return order


def _build(provider_key: str) -> ASRConfig | None:
    """按 provider 键构建配置；未配对应 Key 时返回 None（表示该 provider 不可用）。"""
    preset = _PROVIDERS.get(provider_key)
    if not preset:
        return None
    api_key = _env(*preset["key_envs"])
    if not api_key:
        return None
    upper = provider_key.upper()
    base_url = _env(f"{upper}_BASE_URL", f"{upper}_ASR_BASE_URL") or preset["base_url"]
    model = _env(f"{upper}_ASR_MODEL", f"{upper}_MODEL") or preset["model"]
    return ASRConfig(
        provider=provider_key,
        label=preset["label"],
        base_url=base_url,
        api_key=api_key,
        model=model,
        style=preset["style"],
        max_bytes=preset["max_bytes"],
    )


def _build_custom() -> ASRConfig | None:
    """完全自定义的 OpenAI 兼容 ASR 端点（需同时给出 ASR_API_KEY + ASR_BASE_URL + ASR_MODEL）。"""
    api_key = _env("ASR_API_KEY")
    base_url = _env("ASR_BASE_URL")
    model = _env("ASR_MODEL")
    if not (api_key and base_url and model):
        return None
    style = _env("ASR_STYLE") or "transcriptions"
    return ASRConfig(
        provider="custom",
        label=_env("ASR_LABEL") or "自定义 ASR",
        base_url=base_url,
        api_key=api_key,
        model=model,
        style=style if style in ("funasr_sse", "transcriptions", "chat_audio") else "transcriptions",
        max_bytes=50 * 1024 * 1024,
    )


def load_asr_chain() -> list[ASRConfig]:
    """返回按优先级排序的可用 ASR provider 链（自定义端点最优先，其后主→备）。"""
    chain: list[ASRConfig] = []
    custom = _build_custom()
    if custom:
        chain.append(custom)
    for provider_key in _resolve_order():
        cfg = _build(provider_key)
        if cfg:
            chain.append(cfg)
    return chain


def asr_available() -> bool:
    """是否已配置可用的 ASR 服务商。"""
    return bool(load_asr_chain())


def asr_label() -> str:
    """当前首选 ASR 服务商展示名（未配置时返回空串）。"""
    chain = load_asr_chain()
    return chain[0].label if chain else ""


# --------------------------------------------------------------------------- #
# 请求实现（两种 OpenAI 兼容风格）
# --------------------------------------------------------------------------- #
def _mime(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "audio/mpeg"


def _post_transcriptions(cfg: ASRConfig, path: Path, language: str | None, timeout: float) -> str:
    """OpenAI 标准 /audio/transcriptions（multipart 直传文件）：硅基流动 / Groq / OpenAI。"""
    endpoint = cfg.base_url.rstrip("/") + "/audio/transcriptions"
    headers = {"Authorization": f"Bearer {cfg.api_key}"}
    data: dict[str, Any] = {"model": cfg.model, "response_format": "json"}
    if language:
        data["language"] = language
    with path.open("rb") as fh:
        files = {"file": (path.name, fh, _mime(path))}
        resp = httpx.post(endpoint, headers=headers, data=data, files=files, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json()
    return (payload.get("text") or "").strip()


def _post_chat_audio(cfg: ASRConfig, path: Path, language: str | None, timeout: float) -> str:
    """OpenAI 兼容 chat.completions + input_audio(base64 Data URL)：阿里云百炼 Qwen3-ASR。"""
    endpoint = cfg.base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"}
    blob = base64.b64encode(path.read_bytes()).decode()
    data_uri = f"data:{_mime(path)};base64,{blob}"
    asr_options: dict[str, Any] = {"enable_itn": True}
    if language:
        asr_options["language"] = language
    payload = {
        "model": cfg.model,
        "messages": [
            {
                "role": "user",
                "content": [{"type": "input_audio", "input_audio": {"data": data_uri}}],
            }
        ],
        "asr_options": asr_options,
        "stream": False,
    }
    resp = httpx.post(endpoint, json=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()
    body = resp.json()
    return (body["choices"][0]["message"]["content"] or "").strip()


# --------------------------------------------------------------------------- #
# Fun-ASR-Flash（百炼原生 HTTP + SSE）：句级时间戳 -> 带时间轴的 segments
# --------------------------------------------------------------------------- #
_AUDIO_FORMAT = {
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/wav": "wav",
    "audio/x-wav": "wav", "audio/opus": "opus", "audio/aac": "aac",
}


def _collect_funasr_events(raw: str) -> list[dict[str, Any]]:
    """从响应体提取 JSON 事件列表：兼容 SSE 流（逐个 data: 行）与普通 JSON（短
    音频一次性返回最终结果）两种形态，保证不同时长音频都解析得到。"""
    stripped = raw.strip()
    if stripped.startswith("{"):
        try:
            return [json.loads(stripped)]
        except ValueError:
            return []
    events: list[dict[str, Any]] = []
    for line in stripped.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        try:
            events.append(json.loads(line[len("data:"):].strip()))
        except ValueError:
            continue   # 非法行（如截断的中间块）：跳过，不影响后续事件
    return events


def _parse_funasr_events(events: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """从 Fun-ASR 事件流提取 (全文, 带时间戳分段)。

    实测语义（与文档示例不同）：``output.sentence`` 是**当前累积句**——每个事件
    的 text / end_time 持续增长（整块音频被视为一个不断增长的句子），
    sentence_end=True 的事件携带词级时间戳 words。故取**最后一个**最终事件，
    从其 words 按句末标点重建句级分段（段起止取首/末词时间戳）。
    """
    text = ""
    final_sentence: dict[str, Any] | None = None
    for ev in events:
        out = (ev.get("output") or {}) if isinstance(ev, dict) else {}
        if out.get("text"):
            text = out["text"]
        s = out.get("sentence") or {}
        if s.get("sentence_end"):
            final_sentence = s   # 后者覆盖前者：取最后一个最终结果
    if final_sentence is None:
        return text, []
    segments = _words_to_segments(final_sentence.get("words") or [])
    if not segments:
        # 无词级时间戳：整块退化为单段（保留块级 begin/end）
        begin = final_sentence.get("begin_time")
        end = final_sentence.get("end_time")
        if (final_sentence.get("text") or "").strip() and begin is not None:
            segments = [{
                "start": round(float(begin) / 1000, 2),
                "end": round(float(end) / 1000, 2) if isinstance(end, (int, float)) and not isinstance(end, bool) else None,
                "text": (final_sentence.get("text") or "").strip(),
            }]
    return text, segments


# 句末标点（含中文）：词级时间戳切句的依据
_SENT_END_PUNCT = set("。！？!?；;…")


def _words_to_segments(words: list[Any]) -> list[dict[str, Any]]:
    """词级时间戳 -> 句级分段：按词后句末标点切分，段起止取首/末词时间戳。"""
    segments: list[dict[str, Any]] = []
    buf: list[dict[str, Any]] = []

    def _flush() -> None:
        if not buf:
            return
        body = "".join((w.get("text") or "") + (w.get("punctuation") or "") for w in buf).strip()
        begin = buf[0].get("begin_time")
        end = buf[-1].get("end_time")
        if body and begin is not None:
            segments.append({
                "start": round(float(begin) / 1000, 2),
                "end": round(float(end) / 1000, 2) if isinstance(end, (int, float)) and not isinstance(end, bool) else None,
                "text": body,
            })
        buf.clear()

    for w in words:
        if not isinstance(w, dict):
            continue
        buf.append(w)
        punct = (w.get("punctuation") or "").strip()
        if punct and punct[-1] in _SENT_END_PUNCT:
            _flush()
    _flush()
    return segments


def _post_funasr_sse(
    cfg: ASRConfig, path: Path, language: str | None, timeout: float
) -> tuple[str, list[dict[str, Any]]]:
    """百炼 Fun-ASR-Flash 非实时识别（原生 HTTP，SSE 开启）：返回带时间戳的分段。

    接口：POST {base_url}/api/v1/services/aigc/multimodal-generation/generation，
    鉴权复用 DASHSCOPE_API_KEY；音频以 base64 Data URI 直传（单次 ≤ 300 秒）。
    """
    endpoint = cfg.base_url.rstrip("/") + "/api/v1/services/aigc/multimodal-generation/generation"
    mime = _mime(path)
    blob = base64.b64encode(path.read_bytes()).decode()
    data_uri = f"data:{mime};base64,{blob}"
    parameters: dict[str, Any] = {"format": _AUDIO_FORMAT.get(mime, "mp3")}
    if language:
        parameters["language_hints"] = [language]   # Fun-ASR 仅支持 1 个语言提示
    payload = {
        "model": cfg.model,
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "input_audio", "input_audio": {"data": data_uri}}],
                }
            ]
        },
        "parameters": parameters,
    }
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
        "X-DashScope-SSE": "enable",
    }
    with httpx.stream("POST", endpoint, json=payload, headers=headers, timeout=timeout) as resp:
        # 先 read() 再 raise_for_status：流式响应未经 read() 时上层读
        # response.text 会抛 ResponseNotRead，错误信息会被吞掉
        raw = resp.read().decode("utf-8", errors="replace")
        if resp.is_error:
            resp.raise_for_status()
    return _parse_funasr_events(_collect_funasr_events(raw))


# Fun-ASR 单次仅处理 ≤ 300s 音频：长音频按 240s 重编码切片逐块转写，时间戳加偏移拼接
_FUNASR_CHUNK_SEC = 240.0


def _probe_duration(path: Path) -> float | None:
    """ffmpeg -i 解析 stderr 的 Duration 行（无 ffmpeg / 解析失败返回 None）。"""
    ffmpeg = _ffmpeg_path()
    if not ffmpeg:
        return None
    try:
        proc = subprocess.run([ffmpeg, "-i", str(path)], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(rb"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr or b"")
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def _split_audio(path: Path, chunk_sec: float) -> list[tuple[Path, float]]:
    """长音频重编码切片（精确到帧，偏移可知）：返回 [(片段路径, 片段起点秒)]。

    单片即可容纳 / 无 ffmpeg 探不到时长时原样返回；某片转码失败则跳过该片
    （对应时间段缺失），不中断整体转写。"""
    dur = _probe_duration(path)
    if dur is None or dur <= chunk_sec:
        return [(path, 0.0)]
    ffmpeg = _ffmpeg_path()
    if not ffmpeg:
        return [(path, 0.0)]
    pieces: list[tuple[Path, float]] = []
    for i, start in enumerate(range(0, ceil(dur), int(chunk_sec))):
        dst = path.parent / f"{path.stem}_part{i}{path.suffix}"
        cmd = [
            ffmpeg, "-y", "-ss", str(start), "-i", str(path), "-t", str(int(chunk_sec)),
            "-ac", "1", "-ar", "16000", "-b:a", "48k", str(dst),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=300)
        except (OSError, subprocess.SubprocessError):
            dst.unlink(missing_ok=True)
            continue
        if proc.returncode == 0 and dst.is_file() and dst.stat().st_size > 0:
            pieces.append((dst, float(start)))
        else:
            dst.unlink(missing_ok=True)
    return pieces or [(path, 0.0)]


def _funasr_transcribe(
    cfg: ASRConfig, path: Path, language: str | None, timeout: float
) -> tuple[str, list[dict[str, Any]]]:
    """Fun-ASR 分块转写：切片 → 逐块调接口 → 时间戳加偏移拼接。"""
    pieces = _split_audio(path, _FUNASR_CHUNK_SEC)
    texts: list[str] = []
    segments: list[dict[str, Any]] = []
    try:
        for piece, offset in pieces:
            text, part = _post_funasr_sse(cfg, piece, language, timeout)
            if text:
                texts.append(text)
            segments.extend(
                {
                    "start": round(s["start"] + offset, 2),
                    "end": round(s["end"] + offset, 2) if s.get("end") is not None else None,
                    "text": s["text"],
                }
                for s in part
            )
    finally:
        for piece, _ in pieces:   # 切片用完即清（原文件由调用方管理）
            if piece != path:
                piece.unlink(missing_ok=True)
    return "\n".join(t for t in texts if t), segments


# --------------------------------------------------------------------------- #
# 文本 -> 分段（无精确时间戳时按句切分，前端可正常展示）
# --------------------------------------------------------------------------- #
_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")


def _synthesize_segments(text: str) -> list[dict[str, Any]]:
    """把整段识别文本按句切分为分段（不含时间戳；start 缺省时前端留空不显示）。"""
    text = (text or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in _SENT_SPLIT.split(text) if p and p.strip()]
    merged: list[str] = []
    buf = ""
    for part in parts:
        buf += part
        if len(buf) >= 20:  # 合并过短碎片，避免过多小段
            merged.append(buf.strip())
            buf = ""
    if buf.strip():
        merged.append(buf.strip())
    return [{"text": m} for m in (merged or [text])]


# --------------------------------------------------------------------------- #
# 对外主入口
# --------------------------------------------------------------------------- #
def transcribe_audio(
    audio_path: str | Path,
    *,
    language: str | None = None,
    timeout: float = 180.0,
) -> dict[str, Any]:
    """对音频文件做语音识别，按 provider 链依次尝试直到成功。

    :param audio_path: 本地音频文件路径（建议 16kHz 单声道 mp3）
    :param language: 可选，指定语种（如 "zh"）提升准确率；留空则自动检测
    :raises ASRNotConfiguredError: 未配置任何可用 provider
    :raises ASRError: 所有 provider 均失败
    :return: {text, segments, provider, language}；Fun-ASR 链路的 segments 带
             start/end 时间戳，其余 provider 按句切分无时间轴（start 缺省）
    """
    chain = load_asr_chain()
    if not chain:
        raise ASRNotConfiguredError("未配置语音识别（ASR）服务，无法转写无字幕视频。")

    path = Path(audio_path)
    if not path.is_file():
        raise ASRError(f"音频文件不存在：{path}")
    size = path.stat().st_size
    lang = language or _env("ASR_LANGUAGE") or None

    errors: list[str] = []
    for cfg in chain:
        if size > cfg.max_bytes:
            errors.append(
                f"{cfg.label}：音频 {size / 1024 / 1024:.1f}MB 超过其 "
                f"{cfg.max_bytes / 1024 / 1024:.0f}MB 上限"
            )
            continue
        try:
            segments: list[dict[str, Any]] = []
            if cfg.style == "funasr_sse":
                text, segments = _funasr_transcribe(cfg, path, lang, timeout)
            elif cfg.style == "chat_audio":
                text = _post_chat_audio(cfg, path, lang, timeout)
            else:
                text = _post_transcriptions(cfg, path, lang, timeout)
        except httpx.HTTPStatusError as exc:
            errors.append(
                f"{cfg.label}：HTTP {exc.response.status_code} {(exc.response.text or '')[:160]}"
            )
            continue
        except httpx.HTTPError as exc:
            errors.append(f"{cfg.label}：网络异常 {exc}")
            continue
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            errors.append(f"{cfg.label}：响应格式异常 {exc}")
            continue
        if not text:
            errors.append(f"{cfg.label}：返回空文本")
            continue
        return {
            "text": text,
            # 无时间戳的 provider（SenseVoice / Qwen3-ASR）按句切分兜底
            "segments": segments or _synthesize_segments(text),
            "provider": cfg.label,
            "language": lang or "",
        }

    raise ASRError("语音识别失败 → " + "；".join(errors))
