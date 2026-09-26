"""视频总结：字幕文本 → 结构化中文摘要。

流程：字幕全文 →（超长则截断）→ 组织提示词 → 调用 LLM → 解析并规整结构化 JSON。
输出：一句话总结、详细摘要、关键要点、章节速览、关键词，供前端渲染。

对未配置大模型 / 调用失败 / 返回不可解析等情况，分别抛出语义清晰的异常，
由 main.py 转成合适的 HTTP 状态码与友好提示（不因 AI 缺失而使服务崩溃）。
"""

from __future__ import annotations

from typing import Any

from backend.ai import config as ai_config
from backend.ai import jsonx
from backend.ai import llm
from backend.ai.config import LLMConfig
from backend import storage  # 总结缓存 + 并发去重（阶段0：SQLite）

# 单次喂给 LLM 的文本上限（主流模型上下文的安全区间，覆盖绝大多数视频）
_MAX_CHARS = 30000

# 提示词版本：修改 _SYSTEM_PROMPT / _build_prompt 后手动 bump，旧总结缓存自然失效
# v2：新增「字符串值内禁未转义英文双引号/禁漏逗号尾随逗号」约束，降低偶发非法 JSON 概率
PROMPT_VERSION = "v2"

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
3. 忠于字幕，不臆造不存在的信息；信息不足以支撑某字段时给出保守概括；
4. JSON 字符串值内如需引用一律用中文引号「」，禁止未转义的英文双引号与反斜杠，禁止漏逗号或尾随逗号。"""


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


def summarize(
    text: str, title: str = "", *, refresh: bool = False, cfg: LLMConfig | None = None
) -> dict[str, Any]:
    """对字幕文本生成结构化总结（带缓存）。

    缓存键 = sha256(文本 + 模型 + PROMPT_VERSION)，跨 URL 复用；命中直接返回
    （cached=True）。相同键的并发请求经 single-flight 只调用一次 LLM。

    :param text: 字幕全文
    :param title: 视频标题（辅助模型理解上下文）
    :param refresh: 为 True 时跳过缓存、强制重算并覆盖
    :param cfg: 调用方解析好的 LLM 配置（用户默认模型覆盖）；None 时走全局 env 配置
    :raises AINotConfiguredError: 未配置大模型
    :raises SummarizeError: 字幕为空、调用失败或解析失败
    """
    if not text or not text.strip():
        raise SummarizeError("字幕文本为空，无法总结。")

    cfg = cfg if cfg is not None else ai_config.load_config()
    if cfg is None:
        raise AINotConfiguredError(
            "尚未配置大模型（缺少 AI_PROVIDER / API Key），无法生成 AI 总结。"
        )

    model_label = f"{cfg.label} · {cfg.model}"
    key = storage.summary_key(text, model_label, PROMPT_VERSION)
    if not refresh:
        hit = storage.repo.get_summary(key)
        if hit is not None:
            hit["cached"] = True
            return hit

    def _do() -> dict[str, Any]:
        # 双重检查：拿到 single-flight 锁后再查一次，避免并发重复调用 LLM
        if not refresh:
            hit = storage.repo.get_summary(key)
            if hit is not None:
                hit["cached"] = True
                return hit
        truncated = len(text) > _MAX_CHARS
        trimmed = text[:_MAX_CHARS]
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_prompt(title, trimmed, truncated)},
        ]
        try:
            data = jsonx.chat_json(cfg, messages, temperature=0.3, max_tokens=4096)
        except llm.LLMError as exc:
            raise SummarizeError(str(exc)) from exc
        except jsonx.JSONExtractError as exc:
            raise SummarizeError(str(exc)) from exc

        result = _normalize(data)
        result["model"] = model_label
        result["truncated"] = truncated
        result["cached"] = False
        storage.repo.put_summary(
            key, result, model=model_label, prompt_version=PROMPT_VERSION, title=title
        )
        return result

    return storage.summary_flight.run(key, _do)
