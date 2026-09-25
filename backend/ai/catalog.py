"""AI 模型目录（阶段3 DB 化）：ai_models 表的种子导入 / 查询 / 管理 CRUD。

演进背景（见 docs/AI模型选择与默认模型设置实现方案.md）：
- 阶段1 目录硬编码于 config.MODEL_CATALOG——与校验/缓存键同源，一致性最强；
- 阶段3（本模块）目录落库 ai_models，管理员可在线增删改（上下架、改展示名、
  调计费倍率），无需发版；MODEL_CATALOG 降级为「出厂种子」。

设计要点：
- 种子幂等：首次访问自动导入，**只补缺不覆盖**——管理员改过的 label/tier/
  enabled 不会被 reseed 重置；
- 进程内缓存：目录读多写少，整表缓存 + 写操作本地失效。阶段A 单进程成立；
  多 worker 部署时改为带 TTL / Redis 广播失效，本模块函数签名不变；
- 兜底不拖垮业务：DB 不可用时查询按空目录处理、is_selectable 放行
  （用户保存设置不因目录故障被锁死），错误只记日志。

运行：python tests/test_ai_models.py
"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Any

from backend.ai.config import MODEL_CATALOG, PROVIDERS
from backend.storage.db import session
from backend.storage.models import AIModel

log = logging.getLogger(__name__)


class CatalogError(ValueError):
    """目录操作校验失败（main.py 映射 HTTP 400）。"""


class CatalogNotFoundError(CatalogError):
    """目录条目不存在（main.py 映射 HTTP 404）。"""


_cache: list[dict[str, Any]] | None = None
_cache_lock = threading.Lock()
_seed_lock = threading.Lock()
_seeded = False


def _new_id() -> str:
    return uuid.uuid4().hex


def _row_to_dict(row: AIModel) -> dict[str, Any]:
    return {
        "id": row.id,
        "provider": row.provider,
        "model": row.model,
        "label": row.label or "",  # 空=未指定展示名（用户侧回退大写 ID）；管理端可见真实值
        "tier": row.tier or "$",
        "price_multiplier": float(row.price_multiplier or 1.0),
        "enabled": bool(row.enabled),
        "sort_order": int(row.sort_order or 0),
    }


def ensure_seeded() -> int:
    """把 config.MODEL_CATALOG 中 DB 缺失的条目补进 ai_models（幂等，只补缺）

    不覆盖已有行：管理员对种子条目的改动（改名/下架/调档）在 reseed 后保留
    :return: 本次实际插入的条数
    """
    inserted = 0
    with _seed_lock:
        with session() as s:
            existing = {(r.provider, r.model) for r in s.query(AIModel).all()}
            for provider, models in MODEL_CATALOG.items():
                if provider not in PROVIDERS:
                    continue  # 种子与预设服务商解耦：只导入可路由调用的条目
                for order, m in enumerate(models):
                    if (provider, m["id"]) in existing:
                        continue
                    s.add(AIModel(
                        id=_new_id(),
                        provider=provider,
                        model=m["id"],
                        label=m.get("label") or "",  # 未指定展示名：留空，展示层回退大写 ID
                        tier=m.get("tier") or "$",
                        price_multiplier=1.0,
                        enabled=True,
                        sort_order=order,
                    ))
                    inserted += 1
    if inserted:
        invalidate()
        log.info("[catalog] 已从出厂种子导入 %d 条模型目录", inserted)
    return inserted


def invalidate() -> None:
    """清空进程内目录缓存（管理写操作后调用；下次查询重新加载）。"""
    global _cache
    with _cache_lock:
        _cache = None


def _ensure_ready() -> None:
    """首次访问时导入种子（一次性；失败不重试轰炸日志）。"""
    global _seeded
    if _seeded:
        return
    try:
        ensure_seeded()
    except Exception as exc:
        log.error("[catalog] 种子导入失败（目录可能为空，不影响业务端点）：%s", exc)
    finally:
        _seeded = True


def _rows() -> list[dict[str, Any]]:
    """整表读取（带进程内缓存）；DB 故障时按空目录处理并保留缓存语义。"""
    global _cache
    _ensure_ready()
    with _cache_lock:
        cached = _cache
    if cached is not None:
        return cached
    try:
        with session() as s:
            rows = [_row_to_dict(r) for r in s.query(AIModel).all()]
    except Exception as exc:
        log.error("[catalog] 读取目录失败（按空目录处理）：%s", exc)
        return []
    with _cache_lock:
        _cache = rows
    return rows


def list_all() -> list[dict[str, Any]]:
    """全量目录（管理端用，含已下架条目），按 (provider, sort_order, model) 排序。"""
    return sorted(_rows(), key=lambda r: (r["provider"], r["sort_order"], r["model"]))


def list_enabled_grouped() -> dict[str, list[dict[str, str]]]:
    """按服务商分组的**已上架**模型（用户「选择模型」弹窗用）。

    返回形如 ``{"deepseek": [{"id": ..., "label": ..., "tier": ...}, ...]}``；
    字段名沿用阶段1 硬编码目录的 id/label/tier，前端零改动。
    label 未指定（空）时回退模型 ID 的大写：glm-5.3 → GLM-5.3。
    """
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in sorted(_rows(), key=lambda r: (r["provider"], r["sort_order"], r["model"])):
        if not row["enabled"] or row["provider"] not in PROVIDERS:
            continue
        grouped.setdefault(row["provider"], []).append(
            {"id": row["model"], "label": row["label"] or row["model"].upper(), "tier": row["tier"]}
        )
    return grouped


def is_selectable(provider: str, model: str) -> bool:
    """该 (provider, model) 是否在目录内且已上架。

    DB 故障时放行（True）——目录不可用不应锁死用户保存设置；运行期调用方
    （ai_override_cfg）同样依赖本函数决定是否回退全局默认。
    """
    return any(
        r["provider"] == provider and r["model"] == model and r["enabled"]
        for r in _rows()
    )


# --------------------------------------------------------------------------- #
# 管理 CRUD（/api/admin/ai/models 端点的业务实现；校验失败抛 ValueError → 400）
# --------------------------------------------------------------------------- #
def _validate(provider: str, model: str, label: str, tier: str,
              price_multiplier: float, sort_order: int) -> None:
    if provider not in PROVIDERS:
        raise CatalogError("不支持的服务商（须为平台 Key 池预设键）。")
    if not model or len(model) > 128:
        raise CatalogError("模型标识须为 1-128 个字符。")
    if label and len(label) > 64:
        raise CatalogError("展示名不可超过 64 个字符（留空则展示模型 ID 的大写）。")
    if not tier or len(tier) > 8:
        raise CatalogError("价格档位不正确。")
    if not 0 < price_multiplier <= 100:
        raise CatalogError("计费倍率须在 (0, 100] 区间。")
    if not -10000 <= sort_order <= 10000:
        raise CatalogError("排序值超出范围。")


def create_model(
    *,
    provider: str,
    model: str,
    label: str = "",
    tier: str = "$",
    price_multiplier: float = 1.0,
    enabled: bool = True,
    sort_order: int = 0,
) -> dict[str, Any]:
    """新增目录条目；(provider, model) 重复时抛 ValueError。

    label 可省略（留空）：用户侧展示时回退模型 ID 的大写。
    """
    provider = (provider or "").strip().lower()
    model = (model or "").strip()
    label = (label or "").strip()
    tier = (tier or "$").strip()
    price_multiplier = float(price_multiplier)
    sort_order = int(sort_order)
    _validate(provider, model, label, tier, price_multiplier, sort_order)
    with session() as s:
        if s.query(AIModel).filter_by(provider=provider, model=model).first() is not None:
            raise CatalogError("该模型已在目录中。")
        row = AIModel(
            id=_new_id(), provider=provider, model=model, label=label, tier=tier,
            price_multiplier=price_multiplier, enabled=bool(enabled), sort_order=sort_order,
        )
        s.add(row)
        s.flush()
        result = _row_to_dict(row)
    invalidate()
    return result


def update_model(model_id: str, **fields: Any) -> dict[str, Any]:
    """更新目录条目的运营属性（label/tier/price_multiplier/enabled/sort_order）。

    provider 与 model 不可改：模型标识变更有「用户已存引用 + 缓存键」两个
    下游，语义上等价于下架旧模型 + 上架新模型，应由调用方分两步完成。
    :raises ValueError: 条目不存在 / 字段取值非法
    """
    allowed = ("label", "tier", "price_multiplier", "enabled", "sort_order")
    extra = set(fields) - set(allowed)
    if extra:
        raise CatalogError(f"不可修改的字段：{', '.join(sorted(extra))}。")
    with session() as s:
        row = s.get(AIModel, (model_id or "").strip())
        if row is None:
            raise CatalogNotFoundError("目录条目不存在。")
        if "label" in fields:
            row.label = str(fields["label"] or "").strip()  # 可清空：恢复「展示大写 ID」
        if "tier" in fields:
            row.tier = str(fields["tier"] or "").strip()
        if "price_multiplier" in fields:
            row.price_multiplier = float(fields["price_multiplier"])
        if "enabled" in fields:
            row.enabled = bool(fields["enabled"])
        if "sort_order" in fields:
            row.sort_order = int(fields["sort_order"])
        # 以合并后的最终值整体校验（含未被本次修改的既有字段）
        _validate(row.provider, row.model, row.label or row.model, row.tier,
                  float(row.price_multiplier or 1.0), int(row.sort_order or 0))
        result = _row_to_dict(row)
    invalidate()
    return result


def delete_model(model_id: str) -> dict[str, Any]:
    """删除目录条目。已存该模型的用户不受影响：ai_override_cfg 检测不到目录
    条目时自动回退全局默认（与「服务商未配 Key」同一兜底语义）。
    
    :raises CatalogNotFoundError: 条目不存在
    """
    with session() as s:
        row = s.get(AIModel, (model_id or "").strip())
        if row is None:
            raise CatalogNotFoundError("目录条目不存在。")
        result = _row_to_dict(row)
        s.delete(row)
    invalidate()
    return result
