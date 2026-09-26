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


def _mask_key(headers: dict[str, str]) -> str:
    """从 Authorization 头提取 Key 尾 4 位（脱敏），便于确认实际用了哪把 Key。"""
    auth = headers.get("Authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else auth.strip()
    return f"***{token[-4:]}" if len(token) >= 4 else "***"


def _auth_hint(status: int, endpoint: str, headers: dict[str, str], payload: dict[str, Any]) -> str:
    """4xx 时附加「端点+模型+Key尾」诊断，帮助定位 BASE_URL / 模型 / Key 是否匹配。

    - 401/403：多为 Key 与端点不匹配（代理 Key 发到官方端点，或反之）。
    - 404    ：多为端点路径或模型名在该服务上不存在（BASE_URL 路径段不对，或模型 id
               非该端点提供）。
    """
    ctx = f"端点={endpoint}｜模型={payload.get('model', '?')}｜Key={_mask_key(headers)}"
    if status in (401, 403):
        return (
            f"（鉴权失败｜{ctx}）请核对该服务商 BASE_URL 与 API Key 是否匹配：模型名为代理"
            "自定义名时，BASE_URL 必须指向同一代理端点，否则 Key 会被官方端点判为无效。"
        )
    if status == 404:
        return (
            f"（未找到｜{ctx}）404 多为「端点路径」或「模型名」在该服务上不存在：请核对 "
            "BASE_URL 路径段（如 /v1 与 /api/v1 之别）及模型 id 是否为该端点实际提供的名称。"
        )
    return ""


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
                hint2 = _auth_hint(exc2.response.status_code, endpoint, headers, retry_payload)
                raise LLMError(f"大模型接口返回 {exc2.response.status_code}：{detail2}{hint2}") from exc2
            except httpx.HTTPError as exc2:
                raise LLMError(f"调用大模型失败（网络异常）：{exc2}") from exc2
        raise LLMError(f"大模型接口返回 {status}：{text[:300]}{_auth_hint(status, endpoint, headers, payload)}") from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"调用大模型失败（网络异常）：{exc}") from exc


def chat_meta(
    cfg: LLMConfig,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.3,
    max_tokens: int = 2048,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """发起一次 chat 补全，返回 ``{content, finish_reason, usage}``。

    ``finish_reason=length`` 表示输出被 max_tokens 截断（结构化 JSON 被拦腰截断的
    常见根因），usage 供成本/长度留痕；排查解析类故障时二者是关键证据。

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
        choice = data["choices"][0]
        return {
            "content": choice["message"]["content"] or "",
            "finish_reason": choice.get("finish_reason"),
            "usage": data.get("usage") or {},
        }
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"大模型响应格式异常：{str(data)[:300]}") from exc


def chat(
    cfg: LLMConfig,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.3,
    max_tokens: int = 2048,
    timeout: float = 120.0,
) -> str:
    """发起一次 chat 补全，返回助手回复文本（``chat_meta`` 的便捷封装）。"""
    return chat_meta(
        cfg, messages, temperature=temperature, max_tokens=max_tokens, timeout=timeout
    )["content"]
