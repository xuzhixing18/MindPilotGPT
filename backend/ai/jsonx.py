"""LLM 结构化输出的稳健 JSON 提取与修复（总结 / 思维导图共用）。

为什么需要：模型**偶发**输出不合法 JSON（漏逗号、尾随逗号、字符串内未转义双引号、
JSON 前后夹带推理/杂文本、截断等）。朴素的 ``json.loads`` 或「首{到尾}」切片在这些问题
面前直接失败，把一次可修复的偶发瑕疵放大成用户可见错误（如
``Expecting ',' delimiter: line 28 column 6``）。

本模块按三级递进尽量在服务端消化：
1. **多候选提取**：代码围栏 → 全文 → 字符串感知的顶层平衡片段（容错夹带杂文本）→ 首{到尾}；
2. **本地轻修复**：补漏逗号、去尾随逗号（字符串感知，不改动字符串内容）；
3. **模型修复重试**：把坏输出作为 assistant 消息回传，要求只输出修复后的合法 JSON（仅失败时触发）。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterator

from backend.ai import llm

log = logging.getLogger(__name__)


class JSONExtractError(ValueError):
    """所有候选与修复手段均失败时抛出（消息可直接面向用户）。"""


def _iter_top_level_spans(text: str) -> Iterator[tuple[int, int]]:
    """字符串感知的顶层 ``{...}`` / ``[...]`` 平衡片段（容错 JSON 前后夹带杂文本/推理）。"""
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            if depth == 0:
                start = i
            depth += 1
        elif ch in "}]":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    yield start, i + 1
                    start = -1


def _candidates(raw: str) -> list[str]:
    """按成功率排序的候选切片（去重保序）。"""
    text = (raw or "").strip()
    out: list[str] = []
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fence:
        out.append(fence.group(1).strip())
    out.append(text)
    out.extend(text[a:b] for a, b in _iter_top_level_spans(text))
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        out.append(text[start:end + 1])
    seen: set[str] = set()
    uniq: list[str] = []
    for cand in out:
        if cand and cand not in seen:
            seen.add(cand)
            uniq.append(cand)
    return uniq


def repair_json(text: str) -> str:
    """本地轻修复：补漏逗号、去尾随逗号（字符串感知，绝不改动字符串内容）。

    覆盖两类高频偶发缺陷：``{"a":1 "b":2}``（漏逗号）与 ``[..., ]``（尾随逗号）。
    串内未转义双引号等歧义缺陷不做猜测性修复，交给模型重试兜底。
    """
    out: list[str] = []
    stack: list[str] = []   # 容器类型："o" 对象 / "a" 数组
    state: list[str] = []   # 与 stack 同长；o: key/colon/value/after，a: value/after
    in_str = False
    esc = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            prev = state[-1] if state else ""
            if prev == "after":  # 上一个值结束后直接来了新 key/新值 → 补逗号
                out.append(",")
                prev = "key" if stack[-1] == "o" else "value"
            out.append(ch)
            i += 1
            while i < n:  # 消费整个字符串字面量
                c2 = text[i]
                out.append(c2)
                if esc:
                    esc = False
                elif c2 == "\\":
                    esc = True
                elif c2 == '"':
                    break
                i += 1
            i += 1
            if stack:
                state[-1] = "colon" if (stack[-1] == "o" and prev == "key") else "after"
            continue
        if ch in "{[":
            stack.append("o" if ch == "{" else "a")
            state.append("key" if ch == "{" else "value")
            out.append(ch)
            i += 1
            continue
        if ch in "}]":
            if stack:
                stack.pop()
                state.pop()
                if state:
                    state[-1] = "after"
            out.append(ch)
            i += 1
            continue
        if ch == ":":
            if stack and state and state[-1] == "colon":
                state[-1] = "value"
            out.append(ch)
            i += 1
            continue
        if ch == ",":
            j = i + 1  # 尾随逗号：其后（跳过空白）若为 } / ] 则丢弃
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1
                continue
            if stack and state:
                state[-1] = "key" if stack[-1] == "o" else "value"
            out.append(ch)
            i += 1
            continue
        if ch in " \t\r\n":
            out.append(ch)
            i += 1
            continue
        if ch in "-0123456789tfn":  # 数字 / true / false / null 字面量
            if stack and state and state[-1] == "after":
                out.append(",")
                state[-1] = "value"
            j = i
            while j < n and text[j] not in ",}] \t\r\n":
                j += 1
            out.append(text[i:j])
            if state:
                state[-1] = "after"
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def extract_json(raw: str) -> Any:
    """多候选 + 本地修复地提取 JSON；全部失败抛 :class:`JSONExtractError`。"""
    candidates = _candidates(raw)
    if not candidates:
        raise JSONExtractError("大模型未返回有效的 JSON 结构。")
    last: json.JSONDecodeError | None = None
    for cand in candidates:
        for attempt in (cand, None):
            text = cand if attempt is not None else repair_json(cand)
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                last = exc
    raise JSONExtractError(f"大模型返回内容无法解析为 JSON：{last}")


_REPAIR_NUDGE = (
    "你上一条回复不是合法 JSON。请只输出修复后的完整合法 JSON："
    "字符串值内不要出现未转义的英文双引号与反斜杠，不要漏逗号、不要尾随逗号，"
    "不要任何解释文字、不要用 Markdown 代码块包裹。"
)


def chat_json(
    cfg: llm.LLMConfig,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    timeout: float = 120.0,
) -> Any:
    """一次「调用 + 稳健解析」：解析失败时自动做一次模型修复重试。

    :raises llm.LLMError: 调用失败（网络 / 鉴权 / 限流）
    :raises JSONExtractError: 重试后仍无法解析（消息可直接面向用户）
    """
    meta = llm.chat_meta(cfg, messages, temperature=temperature, max_tokens=max_tokens, timeout=timeout)
    raw = meta["content"]
    try:
        return extract_json(raw)
    except JSONExtractError as exc:
        log.warning(
            "[jsonx] 首次解析失败 finish_reason=%s err=%s raw_head=%r",
            meta.get("finish_reason"), exc, raw[:300],
        )
        repair_msgs = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": _REPAIR_NUDGE},
        ]
        meta2 = llm.chat_meta(cfg, repair_msgs, temperature=0.0, max_tokens=max_tokens, timeout=timeout)
        return extract_json(meta2["content"])
