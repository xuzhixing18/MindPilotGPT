"""OpenAI 兼容的 Chat Completions 调用（httpx）。

DeepSeek / 智谱 / 通义 / Kimi / OpenAI 均提供 OpenAI 兼容的 /chat/completions，
因此这里用一份 httpx 调用即可覆盖全部服务商，端点与模型由 LLMConfig 决定。
不绑定任何厂商 SDK，保持轻量与可替换。
"""

from __future__ import annotations

from typing import Any

import httpx

from backend.ai.config import LLMConfig


class LLMError(RuntimeError):
    """调用大模型失败（网络 / 鉴权 / 限流 / 响应格式）。"""


# 部分模型（推理类如 Kimi-k2 / OpenAI o 系列，或某些服务商）不接受自定义采样参数，
# 400 错误文本命中这些关键词时，移除 temperature / max_tokens 用服务商默认值重试一次。
_SAMPLING_PARAM_HINTS = ("temperature", "max_tokens", "top_p", "unsupported", "not support")


def _is_sampling_param_error(status: int, text: str) -> bool:
    """判断 400 是否由不被支持的采样参数（temperature 等）引起。"""
    if status != 400:
        return False
    low = (text or "").lower()
    return any(hint in low for hint in _SAMPLING_PARAM_HINTS)


def _post(endpoint: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> Any:
    """POST 请求；对采样参数不兼容自动降级重试，成功时返回响应 JSON。"""
    try:
        resp = httpx.post(endpoint, json=payload, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        text = exc.response.text or ""
        # 降级重试：移除可选采样参数，用服务商默认值再试一次
        if _is_sampling_param_error(status, text) and (
            "temperature" in payload or "max_tokens" in payload
        ):
            retry_payload = {
                k: v for k, v in payload.items() if k not in ("temperature", "max_tokens")
            }
            try:
                resp2 = httpx.post(endpoint, json=retry_payload, headers=headers, timeout=timeout)
                resp2.raise_for_status()
                return resp2.json()
            except httpx.HTTPStatusError as exc2:
                detail2 = (exc2.response.text or "")[:300]
                raise LLMError(f"大模型接口返回 {exc2.response.status_code}：{detail2}") from exc2
            except httpx.HTTPError as exc2:
                raise LLMError(f"调用大模型失败（网络异常）：{exc2}") from exc2
        raise LLMError(f"大模型接口返回 {status}：{text[:300]}") from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"调用大模型失败（网络异常）：{exc}") from exc


def chat(
    cfg: LLMConfig,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.3,
    max_tokens: int = 2048,
    timeout: float = 120.0,
) -> str:
    """发起一次 chat 补全，返回助手回复文本。

    :raises LLMError: HTTP 错误、网络异常或响应结构异常
    """
    endpoint = cfg.base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "model": cfg.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    data = _post(endpoint, headers, payload, timeout)
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"大模型响应格式异常：{str(data)[:300]}") from exc
