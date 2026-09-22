"""视频总结：字幕文本 → 结构化中文摘要。

流程：字幕全文 →（超长则截断）→ 组织提示词 → 调用 LLM → 解析并规整结构化 JSON。
输出：一句话总结、详细摘要、关键要点、章节速览、关键词，供前端渲染。

对未配置大模型 / 调用失败 / 返回不可解析等情况，分别抛出语义清晰的异常，
由 main.py 转成合适的 HTTP 状态码与友好提示（不因 AI 缺失而使服务崩溃）。
"""

from __future__ import annotations

import json
import re
from typing import Any

from backend.ai import config as ai_config
from backend.ai import llm

# 单次喂给 LLM 的文本上限（主流模型上下文的安全区间，覆盖绝大多数视频）
_MAX_CHARS = 30000

_SYSTEM_PROMPT = (
    "你是一位专业的视频内容分析助手，擅长把口语化、可能含识别错误的字幕，"
    "提炼成准确、结构化的中文摘要。你只依据给定字幕内容作答，绝不编造。"
)


class AINotConfiguredError(RuntimeError):
    """未配置大模型（AI_PROVIDER / API Key 缺失）。"""


class SummarizeError(RuntimeError):
    """总结失败（LLM 调用异常或结果无法解析）。"""


def _build_prompt(title: str, text: str, truncated: bool) -> str:
    note = "（注意：字幕过长，以下仅为前半部分，请据此概括）" if truncated else ""
    return f"""请阅读下面的视频字幕，输出该视频的结构化总结。

视频标题：{title or "（无标题）"}
{note}
字幕全文：
\"\"\"
{text}
\"\"\"

请严格输出如下 JSON（不要输出任何 JSON 以外的文字，也不要用 Markdown 代码块包裹）：
{{
  "one_line": "一句话概括视频核心（不超过 40 字）",
  "summary": "详细摘要，150~300 字，覆盖视频主要内容与结论",
  "key_points": ["关键要点1", "关键要点2", "关键要点3"],
  "chapters": [
    {{"title": "章节/主题名", "summary": "该部分讲了什么（1~2 句）"}}
  ],
  "keywords": ["关键词1", "关键词2"]
}}

要求：
1. 全部使用简体中文；
2. key_points 提炼 3~8 条，chapters 按内容顺序给出 2~6 个；
3. 忠于字幕，不臆造不存在的信息；信息不足以支撑某字段时给出保守概括。"""


def _extract_json(raw: str) -> dict[str, Any]:
    """从 LLM 回复里稳健地提取 JSON 对象（容错 ```json 围栏与前后杂文本）。"""
    text = (raw or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 退化：截取第一个 { 到最后一个 }
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise SummarizeError(f"大模型返回内容无法解析为 JSON：{exc}") from exc
    raise SummarizeError("大模型未返回有效的 JSON 结构。")


def _normalize(data: dict[str, Any]) -> dict[str, Any]:
    """规整 LLM 输出，保证前端拿到的字段类型稳定（容错缺字段/类型不符）。"""
    def _str_list(val: Any) -> list[str]:
        if isinstance(val, list):
            return [str(x).strip() for x in val if str(x).strip()]
        if isinstance(val, str) and val.strip():
            return [val.strip()]
        return []

    chapters: list[dict[str, str]] = []
    raw_chapters = data.get("chapters")
    if isinstance(raw_chapters, list):
        for ch in raw_chapters:
            if isinstance(ch, dict):
                chapters.append({
                    "title": str(ch.get("title") or "").strip(),
                    "summary": str(ch.get("summary") or "").strip(),
                })
            elif isinstance(ch, str) and ch.strip():
                chapters.append({"title": ch.strip(), "summary": ""})

    return {
        "one_line": str(data.get("one_line") or "").strip(),
        "summary": str(data.get("summary") or "").strip(),
        "key_points": _str_list(data.get("key_points")),
        "chapters": chapters,
        "keywords": _str_list(data.get("keywords")),
    }


def summarize(text: str, title: str = "") -> dict[str, Any]:
    """对字幕文本生成结构化总结。

    :param text: 字幕全文
    :param title: 视频标题（辅助模型理解上下文）
    :raises AINotConfiguredError: 未配置大模型
    :raises SummarizeError: 字幕为空、调用失败或解析失败
    """
    if not text or not text.strip():
        raise SummarizeError("字幕文本为空，无法总结。")

    cfg = ai_config.load_config()
    if cfg is None:
        raise AINotConfiguredError(
            "尚未配置大模型（缺少 AI_PROVIDER / API Key），无法生成 AI 总结。"
        )

    truncated = len(text) > _MAX_CHARS
    trimmed = text[:_MAX_CHARS]
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _build_prompt(title, trimmed, truncated)},
    ]
    try:
        raw = llm.chat(cfg, messages, temperature=0.3, max_tokens=2048)
    except llm.LLMError as exc:
        raise SummarizeError(str(exc)) from exc

    result = _normalize(_extract_json(raw))
    result["model"] = f"{cfg.label} · {cfg.model}"
    result["truncated"] = truncated
    return result
