"""library 配置：历史 / 合集 / 问答会话的上限与截断（全部 env 驱动，有默认值）。

与项目既有配置范式一致：读取函数每次调用读 env（测试可运行时改 env 生效），
解析失败回退默认值，不抛错。
"""

from __future__ import annotations

import os

# 合集名长度上限（与 models.Collection.name 的 String(40) 对齐）
COLLECTION_NAME_MAX = 40

# 单条问答消息内容上限（防超长粘贴撑爆上下文与存储）
MESSAGE_MAX_CHARS = 2000


def _int(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or str(default)).strip())
    except ValueError:
        return default


def history_max_entries() -> int:
    """每用户历史条目上限，超限滚动淘汰最旧（默认 500）。"""
    return _int("HISTORY_MAX_ENTRIES", 500)


def collections_max_per_user() -> int:
    """每用户合集数上限（默认 50）。"""
    return _int("COLLECTIONS_MAX_PER_USER", 50)


def collection_items_max() -> int:
    """单合集条目上限（默认 200）。"""
    return _int("COLLECTION_ITEMS_MAX", 200)


def qa_sessions_max_per_user() -> int:
    """每用户问答会话上限，超限滚动淘汰最旧不活跃（默认 50）。"""
    return _int("QA_SESSIONS_MAX_PER_USER", 50)


def qa_max_history_messages() -> int:
    """服务端组装 QA 上下文的最大历史条数（默认 8，原 ai.qa 前端 slice 策略收归）。"""
    return _int("QA_MAX_HISTORY_MESSAGES", 8)
