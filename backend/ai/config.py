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

# 尽力加载 .env（未安装 python-dotenv 时静默跳过，仍可用系统环境变量）
try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")
except ImportError:  # pragma: no cover - python-dotenv 可选
    pass


# 各服务商的 OpenAI 兼容端点、默认模型与专属 Key 环境变量名（按优先级尝试多个别名）
PROVIDERS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "key_envs": ["DEEPSEEK_API_KEY"],
        "label": "DeepSeek",
    },
    "zhipu": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "key_envs": ["ZHIPU_API_KEY", "GLM_API_KEY", "BIGMODEL_API_KEY"],
        "label": "智谱 GLM",
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "key_envs": ["DASHSCOPE_API_KEY", "QWEN_API_KEY"],
        "label": "通义千问",
    },
    "kimi": {
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "key_envs": ["MOONSHOT_API_KEY", "KIMI_API_KEY"],
        "label": "Kimi (Moonshot)",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "key_envs": ["OPENAI_API_KEY"],
        "label": "OpenAI",
    },
}

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


def load_config() -> LLMConfig | None:
    """从环境变量解析当前 LLM 配置；未配置 Key 时返回 None（表示 AI 不可用）。

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
    base_url = _env("AI_BASE_URL", "LLM_BASE_URL") or preset["base_url"]
    # model 优先级：显式 AI_MODEL/LLM_MODEL > 标识里的模型部分 > 服务商默认
    model = _env("AI_MODEL", "LLM_MODEL") or model_override or preset["model"]
    return LLMConfig(provider, preset["label"], base_url, api_key, model)


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
