"""语音识别（ASR）兜底 —— provider 链，config-gated。

用途：当视频抓不到字幕时（抖音无字幕轨、B站未登录等），下载音频调用第三方
ASR 转写为文本，作为字幕提取的兜底。与 backend.ai 的多服务商风格一致：

- 主：硅基流动 SenseVoiceSmall —— OpenAI 兼容 ``/audio/transcriptions``（multipart
  直传文件），完全免费不限量，中文优化，单文件 ≤50MB / ≤1h；
- 备：阿里云百炼 Qwen3-ASR-Flash —— OpenAI 兼容 ``chat.completions`` + ``input_audio``
  （base64 Data URL），base64 编码后 ≤10MB。

按 env 组成「可用 provider 链」并依次尝试直到成功：未配硅基流动 Key 时自动只用
DashScope，两者都配时优先硅基流动。未配置任何 Key 时 ``asr_available()`` 为 False，
上层降级为「无字幕」提示（不报错）。密钥只从环境变量 / .env 读取，绝不硬编码。

也支持完全自定义的 OpenAI 兼容 ASR 端点（ASR_API_KEY + ASR_BASE_URL + ASR_MODEL），
便于随时切换到 Groq / OpenAI / 自建服务等。
"""

from __future__ import annotations

import base64
import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

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


# 两种调用风格：
#   transcriptions —— OpenAI 标准 /audio/transcriptions（multipart 直传文件）：硅基流动 / Groq / OpenAI
#   chat_audio     —— OpenAI 兼容 chat.completions + input_audio(base64 Data URL)：阿里云百炼 Qwen3-ASR
_PROVIDERS: dict[str, dict[str, Any]] = {
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
    "sensevoice": "siliconflow", "sf": "siliconflow", "siliconcloud": "siliconflow",
    "qwen": "dashscope", "qwen-asr": "dashscope", "qwen3-asr": "dashscope",
    "tongyi": "dashscope", "aliyun": "dashscope", "bailian": "dashscope",
}

# 默认尝试顺序（主 → 备）
_DEFAULT_ORDER = ["siliconflow", "dashscope"]


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
        style=style if style in ("transcriptions", "chat_audio") else "transcriptions",
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
    :return: {text, segments, provider, language}
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
            if cfg.style == "chat_audio":
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
            "segments": _synthesize_segments(text),
            "provider": cfg.label,
            "language": lang or "",
        }

    raise ASRError("语音识别失败 → " + "；".join(errors))
