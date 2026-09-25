"""视频思维导图：字幕文本 → 层级化树结构（供前端渲染脑图）。

流程与 ``summary.py`` 同构：字幕全文 →（超长则截断）→ 组织提示词 → 调用 LLM →
解析并规整为「根主题 + 多级子节点」的树。输出稳定字段供前端递归渲染。

复用既有基建：
- 缓存键 ``storage.mindmap_key``（文本 + 模型 + PROMPT_VERSION）与 ``mindmap_flight``
  并发去重，跨 URL / 跨用户复用（思维导图是昂贵且可复用的资产）；
- 未配置大模型时抛 ``AINotConfiguredError``（与总结一致，main.py 转 503）。
"""

from __future__ import annotations

import json
import re
from typing import Any

from backend import storage  # 思维导图缓存 + 并发去重（阶段0：SQLite）
from backend.ai import config as ai_config
from backend.ai import llm
from backend.ai.config import LLMConfig
from backend.ai.summary import AINotConfiguredError  # 复用「未配置大模型」语义化异常

# 单次喂给 LLM 的文本上限（与总结一致，覆盖绝大多数视频）
_MAX_CHARS = 30000

# 提示词版本：修改 _SYSTEM_PROMPT / _build_prompt 后手动 bump，旧缓存自然失效
PROMPT_VERSION = "v1"

# 树的规模上限：限制深度与每层宽度，保证前端可渲染、响应体可控
_MAX_DEPTH = 4
_MAX_CHILDREN = 8

_SYSTEM_PROMPT = (
    "你是一位专业的知识结构化助手，擅长把口语化、可能含识别错误的字幕，"
    "梳理成层级清晰、忠于原文的思维导图。你只依据给定字幕内容作答，绝不编造。"
)


class MindmapError(RuntimeError):
    """思维导图生成失败（LLM 调用异常或结果无法解析）。"""


def _build_prompt(title: str, text: str, truncated: bool) -> str:
    note = "（注意：字幕过长，以下仅为前半部分，请据此梳理）" if truncated else ""
    return f"""请阅读下面的视频字幕，梳理出一张层级清晰的思维导图。

视频标题：{title or "（无标题）"}
{note}
字幕全文：
\"\"\"
{text}
\"\"\"

请严格输出如下 JSON（不要输出任何 JSON 以外的文字，也不要用 Markdown 代码块包裹）：
{{
  "title": "思维导图中心主题（不超过 20 字）",
  "children": [
    {{
      "title": "一级分支（核心板块 / 主题）",
      "children": [
        {{"title": "二级要点（具体观点 / 论据）", "children": []}}
      ]
    }}
  ]
}}

要求：
1. 全部使用简体中文，节点标题精炼（每个不超过 24 字）；
2. 一级分支 3~6 个，每个分支下二级要点 2~5 个，最多到三级；
3. 忠于字幕内容，不臆造不存在的信息；叶子节点的 children 用空数组 []；
4. 中心主题应概括整段视频主旨，而非照抄标题。"""


def _extract_json(raw: str) -> Any:
    """从 LLM 回复里稳健地提取 JSON（容错 ```json 围栏、前后杂文本、根为数组）。"""
    text = (raw or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 退化：截取第一个 { 到最后一个 }，或第一个 [ 到最后一个 ]
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start, end = text.find(open_c), text.rfind(close_c)
        if 0 <= start < end:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError as exc:
                raise MindmapError(f"大模型返回内容无法解析为 JSON：{exc}") from exc
    raise MindmapError("大模型未返回有效的 JSON 结构。")


def _find_root(data: Any) -> Any:
    """定位树根：兼容 LLM 把树包在 root/mindmap/tree/data 里，或直接返回数组。"""
    if isinstance(data, list):
        return {"title": "", "children": data}
    if isinstance(data, dict):
        for k in ("root", "mindmap", "tree", "data"):
            inner = data.get(k)
            if isinstance(inner, (dict, list)):
                return _find_root(inner)
        return data
    return {"title": "", "children": []}


def _normalize_node(node: Any, depth: int = 0) -> dict[str, Any] | None:
    """递归规整节点：统一为 {title, children}，并裁剪超深 / 超宽 / 空节点。"""
    if isinstance(node, str):
        title = node.strip()
        return {"title": title, "children": []} if title else None
    if not isinstance(node, dict):
        return None

    title = str(
        node.get("title") or node.get("name") or node.get("text") or node.get("label") or ""
    ).strip()

    children: list[dict[str, Any]] = []
    raw_children = node.get("children")
    if raw_children is None:
        for k in ("nodes", "sub", "items", "child"):
            if isinstance(node.get(k), list):
                raw_children = node[k]
                break
    if isinstance(raw_children, list) and depth < _MAX_DEPTH:
        for child in raw_children[:_MAX_CHILDREN]:
            nc = _normalize_node(child, depth + 1)
            if nc is not None:
                children.append(nc)

    if not title and not children:
        return None
    return {"title": title, "children": children}


def build_mindmap(
    text: str, title: str = "", *, refresh: bool = False, cfg: LLMConfig | None = None
) -> dict[str, Any]:
    """对字幕文本生成层级思维导图（带缓存）。

    缓存键 = sha256(文本 + 模型 + PROMPT_VERSION)，跨 URL 复用；命中直接返回
    （cached=True）。相同键的并发请求经 single-flight 只调用一次 LLM。

    :param text: 字幕全文
    :param title: 视频标题（辅助模型理解上下文，并作为根主题兜底）
    :param refresh: 为 True 时跳过缓存、强制重算并覆盖
    :param cfg: 调用方解析好的 LLM 配置（用户默认模型覆盖）；None 时走全局 env 配置
    :raises AINotConfiguredError: 未配置大模型
    :raises MindmapError: 字幕为空、调用失败或解析失败
    :return: {title, children, model, truncated, cached}
    """
    if not text or not text.strip():
        raise MindmapError("字幕文本为空，无法生成思维导图。")

    cfg = cfg if cfg is not None else ai_config.load_config()
    if cfg is None:
        raise AINotConfiguredError(
            "尚未配置大模型（缺少 AI_PROVIDER / API Key），无法生成思维导图。"
        )

    model_label = f"{cfg.label} · {cfg.model}"
    key = storage.mindmap_key(text, model_label, PROMPT_VERSION)
    if not refresh:
        hit = storage.repo.get_mindmap(key)
        if hit is not None:
            hit["cached"] = True
            return hit

    def _do() -> dict[str, Any]:
        # 双重检查：拿到 single-flight 锁后再查一次，避免并发重复调用 LLM
        if not refresh:
            hit = storage.repo.get_mindmap(key)
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
            raw = llm.chat(cfg, messages, temperature=0.3, max_tokens=2048)
        except llm.LLMError as exc:
            raise MindmapError(str(exc)) from exc

        root = _normalize_node(_find_root(_extract_json(raw)))
        if root is None:
            raise MindmapError("大模型未返回有效的思维导图结构。")
        result = {
            "title": root["title"] or (title or "视频思维导图"),
            "children": root["children"],
            "model": model_label,
            "truncated": truncated,
            "cached": False,
        }
        storage.repo.put_mindmap(
            key, result, model=model_label, prompt_version=PROMPT_VERSION, title=title
        )
        return result

    return storage.mindmap_flight.run(key, _do)
