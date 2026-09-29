"""notes 业务编排：marks 解析、截断、滚动淘汰、级联删除、自动保存冲突、图片认领与清扫。

路由只调本模块门面；语义化异常直接冒泡，由 ``router.notes_error_handler`` 映射 HTTP。
历史 kinds 联动复用 library 既有钩子（record_action / history_remove_kind），notes
不重复实现归属与时间线逻辑。
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from backend.library import service as library_service
from backend.notes import config, images, store
from backend.notes.errors import (
    CapExceededError,
    ConflictError,
    NotFoundError,
    ValidationError,
)

log = logging.getLogger(__name__)

# 时间戳标记：@[03:12](t=192)（前后端单一约定，见 config.TS_MARK_RE）
_TS_MARK = re.compile(config.TS_MARK_RE)

# 历史内容类型联动：写入时打 note 徽标（library.record_action 校验 KINDS，需先扩展）
NOTE_KIND = "note"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: str | None) -> datetime | None:
    """ISO 串 → aware datetime（base_updated_at 冲突检测用）；失败返回 None。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_marks(body: str) -> list[dict[str, Any]]:
    """从 Markdown 正文解析时间戳标记 → [{t, label}]（服务端事实源，不信任客户端）。"""
    marks: list[dict[str, Any]] = []
    for m in _TS_MARK.finditer(body or ""):
        try:
            t = float(m.group(2))
        except ValueError:
            continue
        marks.append({"t": t, "label": m.group(1)})
    # 去重（同一时间点只留首个）并按时间升序：时间线聚合与导出都是时间序
    seen: set[float] = set()
    unique = [mk for mk in marks if not (mk["t"] in seen or seen.add(mk["t"]))]
    unique.sort(key=lambda mk: mk["t"])
    return unique


# --------------------------------------------------------------------------- #
# 笔记 CRUD
# --------------------------------------------------------------------------- #
def _clean_body(body: str) -> tuple[str, bool]:
    """截断正文至上限（容忍截断优于拒绝提交）；返回 (body, truncated)。"""
    text = (body or "").replace("\r\n", "\n")
    limit = config.note_max_chars()
    return (text, False) if len(text) <= limit else (text[:limit], True)


def _validate_content_key(content_key: str) -> str:
    cleaned = (content_key or "").strip()
    if not cleaned:
        raise ValidationError("content_key 不能为空。")
    return cleaned


def _roll_notes(user_id: str) -> None:
    """超限滚动淘汰最旧不活跃（淘汰语义与用户删除一致：级联其图片）。"""
    over = store.note_count(user_id) - config.notes_max_per_user()
    if over <= 0:
        return
    for note_id in store.note_oldest_ids(user_id, over):
        delete_note(user_id, note_id)


def _sync_note_kind(user_id: str, content_key: str) -> None:
    """该视频已无笔记时，从历史 kinds 移除 note（与 library._sync_qa_kind 同一做法）。"""
    if store.note_ids_for_content(user_id, content_key):
        return
    library_service.store.history_remove_kind(user_id, content_key, NOTE_KIND)


def create_note(
    user_id: str,
    content_key: str,
    body: str,
    url: str = "",
    title: str = "",
    tag: str = "",
    source_type: str = "manual",
    source_ref: str = "",
) -> dict[str, Any]:
    """新建笔记：截断正文、解析 marks、认领孤儿图片、写历史 note 徽标、滚动淘汰。"""
    key = _validate_content_key(content_key)
    text, truncated = _clean_body(body)
    note = store.note_create(
        user_id, key, text, url=url or "", title=title or "",
        marks=parse_marks(text), tag=tag or "", source_type=source_type, source_ref=source_ref,
    )
    # 认领该视频下「新建未保存时贴的图」（note_id 为空的孤儿行）；
    # 认领后回写冗余计数列（否则 get_note 读行仍是 0，列表徽标错误）
    claimed = store.image_claim_for_content(user_id, note["id"], key)
    if claimed:
        _touch_note_image_count(user_id, note["id"])
    note["image_count"] = store.image_count_for_note(user_id, note["id"])
    if url or title:
        library_service.record_action(
            user_id, url=url, title=title, kind=NOTE_KIND, content_key=key,
        )
    _roll_notes(user_id)   # 创建后再滚动：新笔记 updated_at 最新，不会被误删
    note["truncated"] = truncated
    note["images_claimed"] = claimed
    _sweep_orphan_images(user_id)
    return note


def list_notes(
    user_id: str,
    content_key: str = "",
    q: str = "",
    tag: str = "",
    starred: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    items, total = store.note_list(
        user_id, content_key=content_key, q=q, tag=tag, starred=starred,
        limit=limit, offset=offset,
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def get_note(user_id: str, note_id: str, with_images: bool = True) -> dict[str, Any]:
    note = store.note_get(user_id, note_id)
    if note is None:
        raise NotFoundError("笔记不存在。")
    if with_images:
        note["images"] = store.images_for_note(user_id, note_id)
    return note


def update_note(
    user_id: str,
    note_id: str,
    body: str | None = None,
    title: str | None = None,
    tag: str | None = None,
    starred: bool | None = None,
    base_updated_at: str | None = None,
) -> dict[str, Any]:
    """自动保存主通道：乐观并发检测 → 截断 → marks 重解析。

    ``base_updated_at`` 为前端上次拿到/写回的 updated_at：库中已更新即 409 并
    携带服务端版本；缺省（老客户端/脚本）跳过检测，保持向后兼容。
    """
    current = store.note_get(user_id, note_id)
    if current is None:
        raise NotFoundError("笔记不存在。")

    base = _parse_dt(base_updated_at)
    if base is not None:
        server_at = _parse_dt(current.get("updated_at"))
        if server_at and server_at > base:
            raise ConflictError("该笔记已在其他窗口被修改。", server=current)

    truncated = False
    clean_body = None
    if body is not None:
        clean_body, truncated = _clean_body(body)
    note = store.note_update(
        user_id, note_id,
        body=clean_body, title=title, tag=tag, starred=starred,
        marks=parse_marks(clean_body) if clean_body is not None else None,
    )
    if note is None:
        raise NotFoundError("笔记不存在。")
    note["truncated"] = truncated
    return note


def delete_note(user_id: str, note_id: str) -> None:
    """删笔记：级联删图片行与磁盘文件；该视频最后一条笔记删除时移除历史 note 徽标。"""
    content_key = store.note_delete(user_id, note_id)
    if content_key is None:
        raise NotFoundError("笔记不存在。")
    for image_id in store.image_delete_rows_by_note(note_id):
        images.delete_image_file(image_id)
    _sync_note_kind(user_id, content_key)


# --------------------------------------------------------------------------- #
# 图片
# --------------------------------------------------------------------------- #
def upload_image(
    user_id: str,
    data: bytes,
    content_key: str,
    t: float | None = None,
    note_id: str | None = None,
) -> dict[str, Any]:
    """传图（截图/贴图共用）：魔数校验落盘 → 落行；返回可直接进 <img src> 的 url。

    ``note_id`` 可空：新建未保存时贴图先落地，create_note 时按 content_key 认领。
    """
    key = _validate_content_key(content_key)
    if note_id is not None and store.note_get(user_id, note_id) is None:
        raise NotFoundError("笔记不存在。")
    if t is not None and not (0 <= float(t) <= 24 * 3600):
        raise ValidationError("截图时间点越界。")

    image_id = uuid.uuid4().hex
    _ext, mime = images.save_image(image_id, data)   # 校验失败抛 ValidationError
    if store.image_total_bytes(user_id) > config.note_image_total_max_bytes():
        # 超总量配额：本次上传回滚（删文件不落行），并惰性清扫后提示
        images.delete_image_file(image_id)
        _sweep_orphan_images(user_id)
        raise CapExceededError("图片存储空间已达上限，请清理旧笔记图片后重试。")

    image = store.image_create(user_id, image_id, key, mime, len(data), t=t, note_id=note_id)
    if note_id:
        _touch_note_image_count(user_id, note_id)
    _sweep_orphan_images(user_id)
    return image


def get_image_content(user_id: str, image_id: str) -> tuple[bytes, str] | None:
    """读取图片内容（鉴权后）；行与文件任一缺失 → None（404）。"""
    if store.image_get(user_id, image_id) is None:
        return None
    return images.read_image(image_id)


def delete_image(user_id: str, image_id: str) -> None:
    """删图（编辑器删图时调用）：删行 + 删文件 + 维持 note.image_count。"""
    images.check_image_id(image_id)
    note_id = store.image_delete(user_id, image_id)
    images.delete_image_file(image_id)   # 文件不存在时幂等跳过
    if note_id is None and store.image_get(user_id, image_id) is None:
        raise NotFoundError("图片不存在。")
    if note_id:
        _touch_note_image_count(user_id, note_id)


def _touch_note_image_count(user_id: str, note_id: str) -> None:
    """把冗余计数 image_count 同步为真实行数（写路径维护，列表免 join）。

    刻意不经 note_update：计数变更不属于用户编辑，不应刷新 updated_at
    （否则会推高冲突基线，另一窗口会被无摹地 409）。
    """
    from sqlalchemy import select

    from backend.storage import models
    from backend.storage.db import session as db_session

    count = store.image_count_for_note(user_id, note_id)
    with db_session() as s:
        row = s.execute(
            select(models.UserNote).where(
                models.UserNote.user_id == user_id, models.UserNote.id == note_id,
            )
        ).scalar_one_or_none()
        if row is not None:
            row.image_count = count


def _sweep_orphan_images(user_id: str) -> None:
    """惰性清扫：删超龄孤儿（note_id 为空且超过 TTL）的行与文件，防磁盘膨胀。

    在写路径上做（每次传图/新建笔记时），不引入后台任务——对齐既有
    「AI 缓存表孤儿导致磁盘膨胀」的教训：孤儿必须在写路径可见即清。
    """
    ttl = 24 * 3600   # 孤儿判定窗口：一天内未被认领的贴图视为放弃
    for image in store.image_orphans(user_id, max_age_seconds=ttl):
        if store.image_delete(user_id, image["id"]) is not None:
            images.delete_image_file(image["id"])
    # 总量仍超配额：仅告警——孤儿之外的图片被正文引用，不能静默删（破坏用户内容）
    if store.image_total_bytes(user_id) > config.note_image_total_max_bytes():
        log.warning("[notes] user=%s 图片总量超配额", user_id)
