"""LLM 配置：多服务商预设 + 环境变量覆盖（模型无关）。

设计目标：DeepSeek / 智谱 GLM / 通义千问 / Kimi / OpenAI 等「OpenAI 兼容」端点
都能通过一份配置切换；新增服务商只需在 PROVIDERS 里加一行，零改调用代码。

安全：所有敏感信息（API Key）只从环境变量 / .env 读取，绝不硬编码或写入代码库。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 项目根目录（backend/ai/config.py → ai → backend → root）
_ROOT = Path(__file__).resolve().parents[2]

# 尽力加载 .env（未安装 python-dotenv 时静默跳过，仍可用系统环境变量）。
# 采用「两阶段」加载：
#   1) 此处常规补缺（override=False，缺省即 False）：AUTH / LOGIN / SESSION 等保持
#     「系统环境变量优先」，不干扰部署注入与测试对这类变量的隔离。
#   2) 下方 PROVIDERS 定义之后，仅对「服务商 / AI」相关变量做 .env 强制覆盖——否则一把
#     陈旧的系统级 KIMI_API_KEY 等会静默遮蔽 .env 里的新 Key（kimi 401 的元凶），出现
#     「明明改了 .env 却不生效」的配置陷阱。
try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")
except ImportError:  # pragma: no cover - python-dotenv 可选
    pass


# 各服务商的 OpenAI 兼容端点、默认模型与专属 Key 环境变量名（按优先级尝试多个别名）
PROVIDERS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-v4-pro",
        "key_envs": ["DEEPSEEK_API_KEY"],
        "base_url_envs": ["DEEPSEEK_BASE_URL"],
        "model_envs": ["DEEPSEEK_MODEL_ID", "DEEPSEEK_MODEL"],
        "label": "DeepSeek",
    },
    "zhipu": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-5.3-flash",
        "key_envs": ["GLM_API_KEY","ZHIPU_API_KEY", "BIGMODEL_API_KEY"],
        "base_url_envs": ["GLM_BASE_URL", "ZHIPU_BASE_URL", "BIGMODEL_BASE_URL"],
        "model_envs": ["GLM_MODEL_ID", "ZHIPU_MODEL_ID", "GLM_MODEL"],
        "label": "智谱 GLM",
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen3.8-flash",
        # key 与 base_url 同序配对：QWEN_* 为代理/中转一对，DASHSCOPE_* 为官方一对。
        # 若两者序错配（如 base_url 取 QWEN_BASE_URL 而 key 取 DASHSCOPE_API_KEY），
        # 会把 A 家的 Key 发到 B 家端点 → 401/404。故 QWEN_API_KEY 需与 QWEN_BASE_URL 同序在前。
        "key_envs": ["QWEN_API_KEY", "DASHSCOPE_API_KEY"],
        "base_url_envs": ["QWEN_BASE_URL", "DASHSCOPE_BASE_URL"],
        "model_envs": ["QWEN_MODEL_ID", "DASHSCOPE_MODEL_ID", "QWEN_MODEL"],
        "label": "千问",
    },
    "kimi": {
        "base_url": "https://api.moonshot.cn/v1",
        "model": "kimi-k2.6",
        "key_envs": ["KIMI_API_KEY","MOONSHOT_API_KEY"],
        "base_url_envs": ["KIMI_BASE_URL", "MOONSHOT_BASE_URL"],
        "model_envs": ["KIMI_MODEL_ID", "MOONSHOT_MODEL_ID", "KIMI_MODEL"],
        "label": "kimi",
    },
    # "openai": {
    #     "base_url": "https://api.openai.com/v1",
    #     "model": "gpt-4o-mini",
    #     "key_envs": ["OPENAI_API_KEY"],
    #     "label": "OpenAI",
    # },
}

# 出厂种子目录（阶段3 DB 化后的语义）：首次访问时由 ai.catalog 导入 ai_models 表
# （幂等，只补缺不覆盖管理员改动），运行期用户侧校验与弹窗渲染均读 DB。
# 此处仍为纯展示数据，**不含任何密钥**：id 为调用值（进 LLM 请求体的 model
# 字段），label 为**可省略**的运营展示名（省略时展示 id 的大写，如 glm-5.3 →
# GLM-5.3；仅当需要翻译/营销命名时才指定，避免与 id 双份事实漂移），tier 为
# 价格档位（$ / $$）。新增服务商时在此补一行种子即可（老库重启后自动补入 DB）。
MODEL_CATALOG: dict[str, list[dict[str, str]]] = {
    "deepseek": [
        {"id": "deepseek-flash", "label": "DeepSeek-Flash", "tier": "$"},
        {"id": "deepseek-v4-pro", "label": "DeepSeek-V4-Pro", "tier": "$$"},
    ],
    "zhipu": [
        {"id": "glm-5.3", "label":"GLM-5.3","tier": "$$$"},
        {"id": "glm-5.3-flash", "label": "GLM-5.3-FLASH", "tier": "$"},
        {"id": "glm-5.2", "label":"GLM-5.2","tier": "$$"},
    ],
    "qwen": [
        {"id": "qwen3.8-max", "label": "Qwen3.8-Max", "tier": "$$$"},
        {"id": "qwen3.8-flash", "label": "Qwen3.8-Flash", "tier": "$"},
        {"id": "qwen3.8-omni-flash", "label": "Qwen3.8-Omni-Flash", "tier": "$"},
    ],
    "kimi": [
        {"id": "kimi-k2.6", "label":"kimi-K2.6","tier": "$"},
        {"id": "kimi-k3", "label":"kimi-K3","tier": "$$$$"},
    ],
    # "openai": [
    #     {"id": "gpt-4o-mini", "label": "GPT-4o mini", "tier": "$"},
    #     {"id": "gpt-4o", "label": "GPT-4o", "tier": "$$"},
    # ],
}


def _ai_override_env_names() -> set[str]:
    """需「.env 优先于系统环境变量」的 AI/服务商变量名（通用 AI_*/LLM_* + 各家 key/base_url/model）。

    集合从 PROVIDERS 派生，新增服务商时无需手动维护，避免与 PROVIDERS 双份事实漂移。
    """
    names = {
        "AI_PROVIDER", "LLM_PROVIDER", "LLM_MODEL_ID",
        "AI_API_KEY", "LLM_API_KEY",
        "AI_MODEL", "LLM_MODEL",
        "AI_BASE_URL", "LLM_BASE_URL",
    }
    for preset in PROVIDERS.values():
        names.update(preset.get("key_envs", ()))
        names.update(preset.get("base_url_envs", ()))
        names.update(preset.get("model_envs", ()))
    return names


# 阶段2：仅对 AI/服务商变量，让 .env 覆盖同名系统/用户级环境变量（见顶部「两阶段」说明）。
# .env 里确有定义且非空的才覆盖；AUTH/LOGIN/SESSION 等其余变量维持系统环境优先。
try:
    from dotenv import dotenv_values

    for _name, _val in dotenv_values(_ROOT / ".env").items():
        if _val and _name in _ai_override_env_names():
            os.environ[_name] = _val
except ImportError:  # pragma: no cover - python-dotenv 可选
    pass

# provider 别名 -> 规范键（让用户按习惯填写：glm/通义/moonshot/gpt 等）
_PROVIDER_ALIASES: dict[str, str] = {
    "glm": "zhipu", "bigmodel": "zhipu",
    "tongyi": "qwen", "qianwen": "qwen", "dashscope": "qwen",
    "moonshot": "kimi",
    "gpt": "openai", "chatgpt": "openai",
}


def _env(*names: str) -> str:
    """按顺序返回第一个非空环境变量值（已去除首尾空白）。"""
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def _resolve_base_url(preset: dict[str, Any]) -> str:
    """解析服务商端点，优先级：按服务商专属变量 > 全局覆盖 > 硬编码预设。

    专属变量（如 KIMI_BASE_URL / QWEN_BASE_URL）让用户能为每家单独配置代理/中转
    端点——多服务商 .env 场景下，避免所有请求被锁死到官方预设端点（否则代理 Key
    发到官方端点会 401）。全局 AI_BASE_URL/LLM_BASE_URL 次之，最后回退预设。
    """
    return (
        _env(*preset.get("base_url_envs", ()))
        or _env("AI_BASE_URL", "LLM_BASE_URL")
        or preset["base_url"]
    )


def _resolve_default_model(preset: dict[str, Any]) -> str:
    """解析服务商默认模型，优先级：按服务商专属变量（如 KIMI_MODEL_ID）> 硬编码预设。"""
    return _env(*preset.get("model_envs", ())) or preset["model"]


def _resolve_provider(raw: str) -> tuple[str, str | None]:
    """把用户填写的服务商/模型标识解析为 (规范 provider, 覆盖 model 或 None)。

    匹配顺序：精确 provider > 别名 > 前缀（“服务商-模型”写法，如 kimi-k3、gpt-4o、
    deepseek-chat）。前缀命中时把原始值作为 model 覆盖，兼顾 LLM_MODEL_ID 的“模型ID”语义。
    """
    key = raw.strip().lower()
    if key in PROVIDERS:
        return key, None
    if key in _PROVIDER_ALIASES:
        return _PROVIDER_ALIASES[key], None
    for name in PROVIDERS:
        if key.startswith(name):
            return name, raw.strip()
    for alias, canon in _PROVIDER_ALIASES.items():
        if key.startswith(alias):
            return canon, raw.strip()
    return key, None  # 未知标识，交由自定义端点分支处理


@dataclass(frozen=True)
class LLMConfig:
    """一次 LLM 调用所需的完整配置。"""

    provider: str
    label: str
    base_url: str
    api_key: str
    model: str


def _load_from_env() -> LLMConfig | None:
    """从环境变量解析全局 LLM 配置；未配置 Key 时返回 None（表示 AI 不可用）。

    支持的环境变量（AI_* 为推荐命名，同时兼容旧的 LLM_* 命名）：
    - AI_PROVIDER / LLM_PROVIDER / LLM_MODEL_ID：选择服务商，默认 deepseek
      （可用别名：glm→zhipu、tongyi→qwen、moonshot→kimi、gpt→openai）
    - AI_API_KEY / LLM_API_KEY：通用 Key（优先级最高）；也可用各家专属变量（见 key_envs）
    - AI_MODEL / LLM_MODEL    ：覆盖默认模型
    - AI_BASE_URL / LLM_BASE_URL：覆盖默认端点（也用于接入未预设的自定义兼容端点）
    """
    raw_provider = _env("AI_PROVIDER", "LLM_PROVIDER", "LLM_MODEL_ID") or "deepseek"
    provider, model_override = _resolve_provider(raw_provider)
    preset = PROVIDERS.get(provider)

    if preset is None:
        # 未知服务商：必须显式给出 base_url + model + key 才启用
        base_url = _env("AI_BASE_URL", "LLM_BASE_URL")
        model = _env("AI_MODEL", "LLM_MODEL") or model_override
        api_key = _env("AI_API_KEY", "LLM_API_KEY")
        if base_url and model and api_key:
            return LLMConfig(provider, provider.title(), base_url, api_key, model)
        return None

    api_key = _env("AI_API_KEY", "LLM_API_KEY", *preset["key_envs"])
    if not api_key:
        return None
    base_url = _resolve_base_url(preset)
    # model 优先级：显式 AI_MODEL/LLM_MODEL > 标识里的模型部分 > 服务商专属默认 > 预设默认
    model = _env("AI_MODEL", "LLM_MODEL") or model_override or _resolve_default_model(preset)
    return LLMConfig(provider, preset["label"], base_url, api_key, model)


def load_config(provider: str | None = None, model: str | None = None) -> LLMConfig | None:
    """解析当前 LLM 配置：用户设置（provider/model 覆盖）优先于环境变量。

    :param provider: 用户选择的规范 provider 键（users.ai_provider）；None=纯 env 行为
    :param model: 用户选择的模型标识（users.ai_model）；None=服务商默认
    :return: 解析出的配置；所选服务商未配 Key 时**回退全局 env**（全局也无 → None）
    """
    if provider is None and model is None:
        return _load_from_env()

    # 用户覆盖路径：仅允许已知预设服务商（PATCH 已校验，此处兜底）；
    # 该服务商 Key 未配置时回退全局配置，避免「选了不可用模型就全 AI 不可用」。
    canon = (provider or "").strip().lower()
    preset = PROVIDERS.get(canon)
    if preset is None:
        return _load_from_env()
    # 用户显式选定服务商：该服务商专属 Key 优先，全局通用 Key（AI_API_KEY/LLM_API_KEY）兜底。
    # 与全局默认路径（通用 Key 优先）相反——「选了谁就用谁的 Key」更符合直觉，避免
    # 全局 Key 被发到不匹配的服务商端点导致 401（Incorrect API key）。
    api_key = _env(*preset["key_envs"], "AI_API_KEY", "LLM_API_KEY")
    if not api_key:
        return _load_from_env()
    base_url = _resolve_base_url(preset)
    resolved_model = (model or "").strip() or _resolve_default_model(preset)
    return LLMConfig(canon, preset["label"], base_url, api_key, resolved_model)


def models_payload(current: tuple[str | None, str | None] | None = None) -> dict[str, Any]:
    """组装「选择模型」弹窗数据：DB 目录（ai_models）+ 可用性 + 全局默认 + 当前选择。

    目录来自 ai.catalog（阶段3 DB 化，管理端可在线增删改），仅返回已上架条目；
    字段结构沿用阶段1（id/label/tier），前端零改动。

    :param current: (provider, model) 登录用户的当前设置；None=未登录/未设置
    """
    from backend.ai import catalog  # 延迟导入：catalog 依赖本模块（种子），避免循环

    default_cfg = _load_from_env()
    grouped = catalog.list_enabled_grouped()
    providers = []
    for key, preset in PROVIDERS.items():
        providers.append({
            "key": key,
            "label": preset["label"],
            "available": bool(_env("AI_API_KEY", "LLM_API_KEY", *preset["key_envs"])),
            "models": grouped.get(key, []),
        })
    return {
        "default": {
            "provider": default_cfg.provider if default_cfg else None,
            "model": default_cfg.model if default_cfg else None,
            "label": default_cfg.label if default_cfg else None,
        },
        "providers": providers,
        "current": {"provider": current[0], "model": current[1]} if current else None,
    }


def ai_available() -> bool:
    """AI 总结是否可用（已正确配置服务商与 Key）。"""
    return load_config() is not None


def current_label() -> str:
    """当前生效的服务商展示名（未配置时返回归一化后的 provider 名）。"""
    cfg = load_config()
    if cfg:
        return cfg.label
    raw = _env("AI_PROVIDER", "LLM_PROVIDER", "LLM_MODEL_ID") or "deepseek"
    return _resolve_provider(raw)[0]
