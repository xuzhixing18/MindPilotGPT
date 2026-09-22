"""视频问答：基于字幕内容的多轮 grounded 问答。

与总结/思维导图不同，问答是**有状态的多轮对话**：前端把历史对话（history）连同
本轮问题一起发来，后端拼装 [系统提示(含字幕) + 历史 + 当前问题] 交给 LLM，约束
模型**只依据字幕内容作答**，超出范围时明确说明「视频中未提及」，避免幻觉。

设计取舍：
- **不做 DB 持久化缓存**：问答是交互式的，每轮上下文（历史）不同、复用率低；
  会话状态由前端在内存中保存（切 Tab / 刷新前保留）。字幕本身已在转写层缓存，
  故每轮问答只需一次 LLM 调用，成本可控。
- 未配置大模型时抛 ``AINotConfiguredError``（与总结/思维导图一致，main.py 转 503）。
"""

from __future__ import annotations

from typing import Any

from backend.ai import config as ai_config
from backend.ai import llm
from backend.ai.summary import AINotConfiguredError  # 复用「未配置大模型」语义化异常

# 喂给 LLM 的字幕上下文上限：需为历史对话与问题留出空间，故略小于总结/思维导图
_MAX_CONTEXT_CHARS = 24000

# 最多携带的历史消息条数（user/assistant 合计），避免上下文无限膨胀
_MAX_HISTORY_MESSAGES = 8

# 单条历史消息内容上限（防止超长粘贴撑爆上下文）
_MAX_HISTORY_ITEM_CHARS = 2000


class QAError(RuntimeError):
    """问答失败（问题为空、LLM 调用异常或返回空）。"""


def _build_system(title: str, text: str, truncated: bool) -> str:
    """构造系统提示：把字幕作为唯一知识来源，约束作答范围与风格。"""
    note = "（注意：字幕过长，以下仅为前半部分）" if truncated else ""
    return (
        "你是视频内容问答助手。下面是一段视频的字幕，请你**只依据字幕内容**回答用户问题。\n"
        "要求：\n"
        "1. 用简体中文作答，简洁、准确、条理清晰（可用分点）；\n"
        "2. 忠于字幕，不编造字幕之外的信息；\n"
        "3. 若字幕中没有相关信息，请明确说明「视频中没有提到相关内容」，不要臆测；\n"
        "4. 可结合上下文对口语化、含识别错误的字幕做合理理解，但不改变原意。\n\n"
        f"视频标题：{title or '（无标题）'}\n"
        f"字幕全文：\n{note}\n\"\"\"\n{text}\n\"\"\""
    )


def _sanitize_history(history: Any) -> list[dict[str, str]]:
    """清洗前端传入的历史对话：只保留 user/assistant 的近若干轮，限长、去空。"""
    if not isinstance(history, list):
        return []
    cleaned: list[dict[str, str]] = []
    for msg in history:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = str(msg.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            cleaned.append({"role": role, "content": content[:_MAX_HISTORY_ITEM_CHARS]})
    return cleaned[-_MAX_HISTORY_MESSAGES:]


def ask(
    text: str,
    title: str = "",
    question: str = "",
    *,
    history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """基于字幕内容回答一个问题（支持多轮上下文）。

    :param text: 字幕全文（作为问答的知识来源）
    :param title: 视频标题（辅助模型理解上下文）
    :param question: 用户本轮问题
    :param history: 之前若干轮对话 [{role: user|assistant, content: str}]，可空
    :raises AINotConfiguredError: 未配置大模型
    :raises QAError: 字幕/问题为空、调用失败或返回空
    :return: {answer, model, question}
    """
    if not text or not text.strip():
        raise QAError("字幕文本为空，无法进行问答。")
    question = (question or "").strip()
    if not question:
        raise QAError("问题为空。")

    cfg = ai_config.load_config()
    if cfg is None:
        raise AINotConfiguredError(
            "尚未配置大模型（缺少 AI_PROVIDER / API Key），无法进行 AI 问答。"
        )

    model_label = f"{cfg.label} · {cfg.model}"
    trimmed = text[:_MAX_CONTEXT_CHARS]
    truncated = len(text) > _MAX_CONTEXT_CHARS

    messages: list[dict[str, str]] = [{"role": "system", "content": _build_system(title, trimmed, truncated)}]
    messages.extend(_sanitize_history(history))
    messages.append({"role": "user", "content": question})

    try:
        raw = llm.chat(cfg, messages, temperature=0.3, max_tokens=1024)
    except llm.LLMError as exc:
        raise QAError(str(exc)) from exc

    answer = (raw or "").strip()
    if not answer:
        raise QAError("大模型返回空回复。")
    return {"answer": answer, "model": model_label, "question": question}
