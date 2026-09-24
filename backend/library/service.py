"""library 业务编排：上限滚动、级联一致性、记录钩子与 QA 会话编排。

路由只调本模块门面；语义化异常直接冒泡，由 ``router.library_error_handler`` 映射 HTTP。
"""

from __future__ import annotations

from typing import Any

from backend.ai import qa as ai_qa
from backend.library import config, store
from backend.library.errors import CapExceededError, NotFoundError, ValidationError
from backend.storage import transcript_key

# 历史记录的内容类型（与前端 Tab 一一对应）
KINDS = ("transcribe", "summary", "mindmap", "comments", "qa")


# --------------------------------------------------------------------------- #
# 处理历史
# --------------------------------------------------------------------------- #
def record_action(
    user_id: str,
    url: str = "",
    title: str = "",
    kind: str = "",
    source: str = "",
    content_key: str | None = None,
) -> dict[str, Any] | None:
    """记录钩子：内容端点成功后调用（main.py 经 BackgroundTask 异步写）。

    未知 kind / 空 user_id 静默跳过（历史记录是辅助数据，不容忍它拖垮主链路）。
    """
    if not user_id or kind not in KINDS:
        return None
    key = content_key or transcript_key(url)
    entry = store.history_upsert(user_id, key, url=url, title=title, kind=kind, source=source)
    _roll_history(user_id)
    return entry


def _roll_history(user_id: str) -> None:
    """超限滚动淘汰最旧（淘汰语义与用户主动删除一致：级联其 QA 会话）。"""
    over = store.history_count(user_id) - config.history_max_entries()
    if over <= 0:
        return
    for key in store.history_oldest(user_id, over):
        delete_history(user_id, key)


def list_history(
    user_id: str,
    q: str = "",
    kind: str = "",
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    items, total = store.history_list(user_id, q=q, kind=kind, limit=limit, offset=offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def get_history(user_id: str, content_key: str) -> dict[str, Any]:
    entry = store.history_get(user_id, content_key)
    if entry is None:
        raise NotFoundError("历史记录不存在。")
    return entry


def delete_history(user_id: str, content_key: str) -> None:
    # 级联：本人该视频的 QA 会话与消息；不动全局缓存与合集条目（见方案 1.3）
    store.qa_delete_sessions_for_content(user_id, content_key)
    if not store.history_delete(user_id, content_key):
        raise NotFoundError("历史记录不存在。")


def clear_history(user_id: str) -> int:
    store.qa_delete_all_sessions(user_id)
    return store.history_clear(user_id)


# --------------------------------------------------------------------------- #
# 合集
# --------------------------------------------------------------------------- #
def _validate_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValidationError("合集名称不能为空。")
    if len(cleaned) > config.COLLECTION_NAME_MAX:
        raise ValidationError(f"合集名称不能超过 {config.COLLECTION_NAME_MAX} 字。")
    return cleaned


def list_collections(user_id: str) -> list[dict[str, Any]]:
    return store.collection_list(user_id)


def create_collection(user_id: str, name: str, description: str = "") -> dict[str, Any]:
    if store.collection_count(user_id) >= config.collections_max_per_user():
        raise CapExceededError(f"合集数量已达上限（{config.collections_max_per_user()} 个）。")
    return store.collection_create(user_id, _validate_name(name), (description or "").strip())


def get_collection(user_id: str, coll_id: str, with_items: bool = False) -> dict[str, Any]:
    coll = store.collection_get(user_id, coll_id)
    if coll is None:
        raise NotFoundError("合集不存在。")
    if with_items:
        coll["items"] = store.item_list(user_id, coll_id, limit=config.collection_items_max())
    else:
        coll["count"] = store.item_count(user_id, coll_id)
    return coll


def update_collection(user_id: str, coll_id: str, name: str | None, description: str | None) -> dict[str, Any]:
    cleaned = _validate_name(name) if name is not None else None
    coll = store.collection_update(user_id, coll_id, cleaned, description)
    if coll is None:
        raise NotFoundError("合集不存在。")
    return coll


def delete_collection(user_id: str, coll_id: str) -> None:
    if not store.collection_delete(user_id, coll_id):
        raise NotFoundError("合集不存在。")


def add_item(user_id: str, coll_id: str, content_key: str, url: str = "", title: str = "") -> dict[str, Any]:
    if not content_key:
        raise ValidationError("content_key 不能为空。")
    if store.item_count(user_id, coll_id) >= config.collection_items_max():
        raise CapExceededError(f"该合集条目已达上限（{config.collection_items_max()} 条）。")
    item = store.item_add(user_id, coll_id, content_key, url=url, title=title)
    if item is None:
        # 区分「合集不存在」与「重复入集」，给出可读提示（两者均为本人视角安全信息）
        if store.collection_get(user_id, coll_id) is None:
            raise NotFoundError("合集不存在。")
        raise ValidationError("该视频已在此合集中。")
    return item


def remove_item(user_id: str, coll_id: str, content_key: str) -> None:
    if not store.item_remove(user_id, coll_id, content_key):
        raise NotFoundError("合集或条目不存在。")


# --------------------------------------------------------------------------- #
# 问答会话
# --------------------------------------------------------------------------- #
def _roll_qa_sessions(user_id: str) -> None:
    over = store.qa_session_count(user_id) - config.qa_sessions_max_per_user()
    while over > 0:
        oldest = store.qa_oldest_session_id(user_id)
        if oldest is None:
            break
        content_key = store.qa_session_delete(user_id, oldest)
        if content_key is not None:
            _sync_qa_kind(user_id, content_key)
        over -= 1


def _sync_qa_kind(user_id: str, content_key: str) -> None:
    """该视频已无 QA 会话时，从历史 kinds 移除 qa。"""
    if store.qa_session_ids_for_content(user_id, content_key):
        return
    store.history_remove_kind(user_id, content_key, "qa")


def create_session(user_id: str, content_key: str, title: str = "") -> dict[str, Any]:
    sess = store.qa_session_create(user_id, content_key, title or "")
    _roll_qa_sessions(user_id)   # 创建后再滚动：新会话 updated_at 最新，不会被误删
    return sess


def list_sessions(user_id: str, content_key: str | None = None) -> list[dict[str, Any]]:
    return store.qa_session_list(user_id, content_key=content_key)


def get_session(user_id: str, session_id: str, with_messages: bool = True) -> dict[str, Any]:
    sess = store.qa_session_get(user_id, session_id)
    if sess is None:
        raise NotFoundError("问答会话不存在。")
    if with_messages:
        sess["messages"] = store.qa_messages(user_id, session_id)
    return sess


def rename_session(user_id: str, session_id: str, title: str) -> dict[str, Any]:
    cleaned = (title or "").strip()
    if not cleaned:
        raise ValidationError("会话名称不能为空。")
    sess = store.qa_session_rename(user_id, session_id, cleaned[:200])
    if sess is None:
        raise NotFoundError("问答会话不存在。")
    return sess


def delete_session(user_id: str, session_id: str) -> None:
    content_key = store.qa_session_delete(user_id, session_id)
    if content_key is None:
        raise NotFoundError("问答会话不存在。")
    _sync_qa_kind(user_id, content_key)


def ask_in_session(
    user_id: str,
    session_id: str | None,
    content_key: str,
    title: str,
    text: str,
    question: str,
    fallback_history: list[dict] | None = None,
) -> tuple[dict[str, Any], str]:
    """会话编排：读 DB 历史组装上下文 → 调 ai.qa.ask → 事务内 append 本轮问答。

    :return: (问答结果, session_id)；session_id 为空时自动新建会话。
    :raises backend.library.errors.NotFoundError: session_id 不属于该用户
    :raises backend.ai 语义化异常: AINotConfiguredError / QAError（main.py 映射 503/502）
    """
    if session_id:
        sess = store.qa_session_get(user_id, session_id)
        if sess is None:
            raise NotFoundError("问答会话不存在。")
    else:
        sess = create_session(user_id, content_key, title)
    sid = sess["id"]

    history = store.qa_recent(user_id, sid, config.qa_max_history_messages()) or fallback_history or None
    result = ai_qa.ask(text, title, question, history=history)

    limit = config.MESSAGE_MAX_CHARS
    store.qa_append(user_id, sid, "user", (question or "")[:limit])
    store.qa_append(user_id, sid, "assistant", (result.get("answer") or "")[:limit])
    record_action(user_id, title=title, kind="qa", content_key=content_key)
    return result, sid


# --------------------------------------------------------------------------- #
# 侧边栏聚合
# --------------------------------------------------------------------------- #
def sidebar(user_id: str) -> dict[str, Any]:
    """侧边栏首屏一次取齐：合集列表 + 最近 20 条历史（免请求瀑布）。"""
    recent, _total = store.history_list(user_id, limit=20)
    return {"collections": store.collection_list(user_id), "recent_history": recent}
