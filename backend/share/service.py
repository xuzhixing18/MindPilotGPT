"""share 业务门面：快照消毒 / 短链生成 / 幂等创建 / 公开响应白名单 / 限流与浏览去重。

快照来源是前端「所见即所享」上报（保证分享物与用户屏幕一致），因此**服务端不信任
任何上报字段**：一律按 config 上限截断、按载体白名单取键。身份字段（标题/封面）以
``video_infos`` 缓存为准、上报值仅兜底，避免客户端伪造他人封面。
"""

from __future__ import annotations

import secrets
import string
import time
from typing import Any
from urllib.parse import quote

from backend.share import config, store
from backend.share.errors import GoneError, NotFoundError, ThrottledError, ValidationError
from backend.storage import normalize_url, repo

_B62 = string.ascii_letters + string.digits

# 进程内限流 / 浏览去重（单节点可接受；分布式阶段换 Redis，接口不变）
_rate_hits: dict[str, list[float]] = {}     # ip -> 命中时间戳列表
_view_seen: dict[str, dict[str, float]] = {}  # code -> {ip: 最近计数时间}


# --------------------------------------------------------------------------- #
# 快照消毒
# --------------------------------------------------------------------------- #
def _clip(value: Any, limit: int) -> str:
    return str(value or "")[:limit]


def _clean_snapshot(kind: str, snap: dict[str, Any], normalized: str) -> tuple[str, dict[str, Any]]:
    """返回 (excerpt, payload)：按载体白名单截断；未知键直接丢弃。"""
    snap = snap if isinstance(snap, dict) else {}
    excerpt = _clip(snap.get("excerpt"), config.EXCERPT_MAX)
    payload: dict[str, Any] = {"normalized_url": normalized}

    bullets = [
        _clip(b, config.BULLET_MAX)
        for b in (snap.get("bullets") or []) if str(b or "").strip()
    ][: config.BULLETS_MAX]
    if bullets:
        payload["bullets"] = bullets

    model_label = _clip(snap.get("model_label"), config.MODEL_LABEL_MAX)
    if model_label:
        payload["model_label"] = model_label

    if kind == "mindmap":
        nodes = [
            _clip(t, config.NODE_MAX)
            for t in (snap.get("top_nodes") or []) if str(t or "").strip()
        ][: config.NODES_MAX]
        if nodes:
            payload["top_nodes"] = nodes
    elif kind == "comments":
        rows = []
        for c in (snap.get("comments") or [])[: config.CMS_MAX]:
            if not isinstance(c, dict):
                continue
            text = _clip(c.get("text"), config.CM_TEXT_MAX)
            if not text:
                continue
            rows.append({
                "author": _clip(c.get("author"), config.CM_AUTHOR_MAX) or "匿名",
                "text": text,
                "likes": max(0, int(c.get("likes") or 0)),
            })
        if rows:
            payload["comments"] = rows
    elif kind == "qa":
        pairs = []
        for p in (snap.get("qa") or [])[: config.QAS_MAX]:
            if not isinstance(p, dict):
                continue
            q, a = _clip(p.get("q"), config.QA_Q_MAX), _clip(p.get("a"), config.QA_A_MAX)
            if q and a:
                pairs.append({"q": q, "a": a})
        if pairs:
            payload["qa"] = pairs
    elif kind == "transcript":
        try:
            payload["segment_count"] = max(0, min(int(snap.get("segment_count") or 0), 10 ** 6))
        except (TypeError, ValueError):
            payload["segment_count"] = 0
    elif kind == "notes":
        try:
            payload["mark_count"] = max(0, min(int(snap.get("mark_count") or 0), 10 ** 4))
        except (TypeError, ValueError):
            payload["mark_count"] = 0
    return excerpt, payload


def _clean_bundle(snap: dict[str, Any], normalized: str) -> tuple[str, dict[str, Any]]:
    """bundle 聚合载体：每个 section 按单载体白名单各自消毒。

    payload.sections = {kind: {excerpt, ...子 payload}}；kinds 按 KINDS 顺序；
    顶层 excerpt/bullets/model_label 取 summary（回退 video/首个 section）——
    保证海报与渠道文案在 bundle 下仍用顶层字段，无需感知多载体结构。
    """
    raw = snap.get("sections") if isinstance(snap.get("sections"), dict) else {}
    sections: dict[str, Any] = {}
    for k in config.KINDS:
        sub = raw.get(k)
        if not isinstance(sub, dict):
            continue
        s_excerpt, s_payload = _clean_snapshot(k, sub, normalized)
        sections[k] = {"excerpt": s_excerpt, **{
            pk: pv for pk, pv in s_payload.items() if pk != "normalized_url"}}
    kinds = [k for k in config.KINDS if k in sections]
    if not kinds:
        raise ValidationError("请至少勾选一种分享内容。")
    payload: dict[str, Any] = {"normalized_url": normalized, "kinds": kinds, "sections": sections}
    for k in ("summary", "video", *kinds):   # 顶层兜底字段：海报/文案兼容
        if not payload.get("excerpt") and k in sections and sections[k].get("excerpt"):
            payload["excerpt"] = sections[k]["excerpt"]
        if not payload.get("bullets") and k in sections and sections[k].get("bullets"):
            payload["bullets"] = sections[k]["bullets"]
        if not payload.get("model_label") and k in sections and sections[k].get("model_label"):
            payload["model_label"] = sections[k]["model_label"]
    return str(payload.get("excerpt") or ""), payload


# --------------------------------------------------------------------------- #
# 创建 / 管理
# --------------------------------------------------------------------------- #
def _new_code() -> str:
    return "".join(secrets.choice(_B62) for _ in range(config.share_code_len()))


def create_share(
    user_id: str,
    content_key: str,
    kind: str = "video",
    url: str = "",
    ref_id: str = "",
    snapshot: dict[str, Any] | None = None,
    force_new: bool = False,
) -> tuple[dict[str, Any], bool]:
    """创建分享（幂等）：返回 (share, created)。

    同 (用户, 视频, 载体, 对象) 已有未撤销分享时复用旧码（链接稳定性优先，
    view 统计不被多码稀释）；``force_new`` 才另发新码。
    bundle 例外：复用同码但**更新内容**——勾选变化只有一条链接，链接不变。
    """
    if kind not in config.ALL_KINDS:
        raise ValidationError(f"不支持的分享载体：{kind}")
    if not (content_key or "").strip():
        raise ValidationError("缺少视频身份键，无法创建分享。")
    ref_id = (ref_id or "").strip() if kind in ("notes", "qa") else ""

    hit = None if force_new else store.share_find_active(user_id, content_key, kind, ref_id)
    if hit and kind != config.BUNDLE_KIND:
        return hit, False   # 非 bundle 幂等快路径：不重读 info、不消毒

    info = repo.get_info(content_key) or {}
    snap = snapshot if isinstance(snapshot, dict) else {}
    title = _clip(info.get("title") or snap.get("title"), config.TITLE_MAX)
    thumb = str(info.get("thumbnail") or "")
    if not thumb.startswith(("http://", "https://")):
        thumb = ""
    normalized = normalize_url(url) if url else str(info.get("normalized_url") or "")
    if kind == config.BUNDLE_KIND:
        excerpt, payload = _clean_bundle(snap, normalized)
    else:
        excerpt, payload = _clean_snapshot(kind, snap, normalized)

    if hit:   # bundle 同码复用：内容随最新勾选更新，链接恒定
        updated = store.share_update(
            hit["id"], title=title, thumb_url=thumb, excerpt=excerpt, payload=payload,
        )
        return updated or hit, False

    for _ in range(5):   # 短链码碰撞重试（概率极低，但唯一约束兜底）
        code = _new_code()
        if store.share_get(code) is None:
            break
    else:
        raise ValidationError("短链码生成失败，请重试。")

    share = store.share_create(
        user_id, code, content_key, kind, ref_id,
        title=title, thumb_url=thumb, excerpt=excerpt, payload=payload,
    )
    return share, True


def list_shares(user_id: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    items, total = store.share_list(user_id, limit=limit, offset=offset)
    return {"items": items, "total": total}


def revoke_share(user_id: str, code: str) -> None:
    if not store.share_revoke(user_id, code):
        raise NotFoundError("分享不存在或已撤销。")


# --------------------------------------------------------------------------- #
# 公开读
# --------------------------------------------------------------------------- #
def get_public(code: str, base_url: str) -> dict[str, Any]:
    """公开快照（字段白名单）：user_id / id / 原始 thumb 域名一律不外泄。"""
    row = store.share_get(code)
    if row is None:
        raise NotFoundError("分享不存在或已失效。")
    if row["revoked_at"]:
        raise GoneError("该分享已被创建者撤销。")
    return public_dict(row, base_url)


def public_dict(row: dict[str, Any], base_url: str) -> dict[str, Any]:
    payload = dict(row.get("payload") or {})
    normalized = payload.get("normalized_url") or ""
    cta = f"{base_url}/#/v?url={quote(normalized)}" if normalized else base_url
    kind = row["kind"]
    if kind == config.BUNDLE_KIND:
        # bundle 深链：含非 video Tab 时直达首个内容 Tab，全为 video 则不带 tab
        tabs = [k for k in (payload.get("kinds") or []) if k != "video"]
        if tabs and normalized:
            cta += f"&tab={tabs[0]}"
    elif kind != "video" and normalized:
        cta += f"&tab={kind}"
    return {
        "code": row["code"],
        "kind": row["kind"],
        "title": row["title"],
        "excerpt": row["excerpt"],
        "thumb_url": f"/api/share/{row['code']}/thumb" if row["thumb_url"] else "",
        "payload": payload,
        "view_count": row["view_count"],
        "created_at": row["created_at"],
        "share_url": f"{base_url}/s/{row['code']}",
        "cta_url": cta,
    }


def thumb_target(code: str) -> str:
    """封面代理目标 URL（只读 shares 快照；撤销 → Gone，不存在 → NotFound）。"""
    row = store.share_get(code)
    if row is None:
        raise NotFoundError("分享不存在或已失效。")
    if row["revoked_at"]:
        raise GoneError("该分享已被创建者撤销。")
    return row["thumb_url"] or ""


def register_view(code: str, ip: str) -> None:
    """浏览计数（IP 窗口去重）：同 IP 窗口内重复浏览只计一次。"""
    now = time.time()
    window = config.view_dedup_seconds()
    seen = _view_seen.get(code)
    if seen is None:
        seen = _view_seen[code] = {}
    elif len(seen) > 5000:   # 惰性清扫：热门分享也 bounded
        cutoff = now - window
        for k in [k for k, v in seen.items() if v < cutoff]:
            seen.pop(k, None)
    last = seen.get(ip)
    if last is not None and now - last < window:
        return
    seen[ip] = now
    store.share_bump_view(code)


def check_rate(ip: str) -> None:
    """公开端点滑动窗口限流（进程内；分布式阶段换 Redis 同语义）。"""
    window = config.rate_window_seconds()
    limit = config.rate_max_per_window()
    now = time.time()
    hits = _rate_hits.get(ip)
    if hits is None:
        hits = _rate_hits[ip] = []
    elif len(hits) > limit * 2:
        hits[:] = [t for t in hits if now - t < window]
    hits.append(now)
    recent = [t for t in hits if now - t < window]
    hits[:] = recent
    if len(recent) > limit:
        raise ThrottledError(retry_after=window)


def reset_throttle_state() -> None:
    """测试钩子：清空限流 / 去重进程内状态。"""
    _rate_hits.clear()
    _view_seen.clear()
