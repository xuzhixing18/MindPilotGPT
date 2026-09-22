"""AI 能力包 —— 大模型抽象层（模型无关，多服务商可切换）。

门面暴露：
    ai.summarize(text, title)  # 结构化视频总结
    ai.ai_available()          # 是否已配置可用
    ai.current_label()         # 当前服务商展示名
    ai.AINotConfiguredError / ai.SummarizeError  # 语义化异常

扩展约定（与 downloader 包一致的「门面 + 可插拔」风格）：
- 新增服务商：只在 config.PROVIDERS 里加一行；
- 新增能力（翻译 / 问答 / 思维导图）：加对应模块并在本门面导出，
  调用方（main.py / 前端）无需感知底层模型差异。
"""

from __future__ import annotations

from backend.ai.config import PROVIDERS, ai_available, current_label, load_config
from backend.ai.summary import AINotConfiguredError, SummarizeError, summarize

__all__ = [
    "summarize",
    "ai_available",
    "current_label",
    "load_config",
    "PROVIDERS",
    "AINotConfiguredError",
    "SummarizeError",
]
