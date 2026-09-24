"""私有资源路由：/api/me/*（历史 / 合集 / 问答会话 / 侧边栏聚合）。

与 auth.router 同风格：路由只调 service 门面，语义化异常冒泡由
``library_error_handler``（main.py 注册）统一映射 HTTP 与 ``{"detail": ...}`` 信封。

门禁：一律 ``Depends(require_user)``——**不看 AUTH_ENABLED 灰度开关**：私有资源
永远要求登录（开关只约束业务端点；AUTH_ENABLED=false 时已登录用户照样可用）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend.auth.dependencies import CurrentUser, require_user
from backend.library import service
from backend.library.errors import CapExceededError, LibraryError, NotFoundError, ValidationError

router = APIRouter(prefix="/api/me", tags=["library"])


# --------------------------------------------------------------------------- #
# 请求体模型
# --------------------------------------------------------------------------- #
class CollectionBody(BaseModel):
    """新建合集。"""

    name: str
    description: str | None = None


class CollectionPatchBody(BaseModel):
    """改合集：只更新显式提交字段。"""

    name: str | None = None
    description: str | None = None


class ItemBody(BaseModel):
    """视频入集：content_key 为视频身份键，url/title 为快照。"""

    content_key: str
    url: str = ""
    title: str = ""


class SessionBody(BaseModel):
    """新建问答会话。"""

    content_key: str
    title: str | None = None


class SessionPatchBody(BaseModel):
    """问答会话改名。"""

    title: str


# --------------------------------------------------------------------------- #
# 语义化异常 → HTTP（404 不确认资源存在，防 IDOR 探测）
# --------------------------------------------------------------------------- #
_STATUS_BY_ERROR: dict[type, int] = {
    NotFoundError: 404,
    ValidationError: 400,
    CapExceededError: 400,
}


def library_error_handler(_request: Request, exc: LibraryError) -> JSONResponse:
    """把 library 语义化异常统一转为 JSONResponse（Starlette 按 MRO 匹配子类）。"""
    return JSONResponse({"detail": str(exc)}, status_code=_STATUS_BY_ERROR.get(type(exc), 400))


# --------------------------------------------------------------------------- #
# 侧边栏聚合
# --------------------------------------------------------------------------- #
@router.get("/sidebar")
def get_sidebar(user: CurrentUser = Depends(require_user)) -> JSONResponse:
    """侧边栏首屏：合集列表 + 最近 20 条历史，一次请求免瀑布。"""
    return JSONResponse(service.sidebar(user.user_id or ""))


# --------------------------------------------------------------------------- #
# 处理历史
# --------------------------------------------------------------------------- #
@router.get("/history")
def list_history(
    q: str = Query("", description="标题/URL 模糊搜索"),
    kind: str = Query("", description="按内容类型过滤：transcribe/summary/mindmap/comments/qa"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: CurrentUser = Depends(require_user),
) -> JSONResponse:
    """历史列表（updated_at 倒序）+ 总数。"""
    return JSONResponse(service.list_history(user.user_id or "", q=q, kind=kind, limit=limit, offset=offset))


@router.get("/history/{content_key}")
def get_history(content_key: str, user: CurrentUser = Depends(require_user)) -> JSONResponse:
    """历史条目详情（kinds 决定结果页 Tab 可用性）；不存在或不属于本人 → 404。"""
    return JSONResponse(service.get_history(user.user_id or "", content_key))


@router.delete("/history/{content_key}")
def delete_history(content_key: str, user: CurrentUser = Depends(require_user)) -> JSONResponse:
    """删历史条目（级联本人该视频的 QA 会话；不动全局缓存与合集条目）。"""
    service.delete_history(user.user_id or "", content_key)
    return JSONResponse({"ok": True})


@router.delete("/history")
def clear_history(user: CurrentUser = Depends(require_user)) -> JSONResponse:
    """清空本人全部历史（同上级联 QA 会话）。"""
    n = service.clear_history(user.user_id or "")
    return JSONResponse({"ok": True, "deleted": n})


# --------------------------------------------------------------------------- #
# 合集
# --------------------------------------------------------------------------- #
@router.get("/collections")
def list_collections(user: CurrentUser = Depends(require_user)) -> JSONResponse:
    return JSONResponse({"collections": service.list_collections(user.user_id or "")})


@router.post("/collections")
def create_collection(body: CollectionBody, user: CurrentUser = Depends(require_user)) -> JSONResponse:
    return JSONResponse(
        {"collection": service.create_collection(user.user_id or "", body.name, body.description or "")},
        status_code=201,
    )


@router.get("/collections/{coll_id}")
def get_collection(coll_id: str, user: CurrentUser = Depends(require_user)) -> JSONResponse:
    """合集详情（含全部条目快照）。"""
    return JSONResponse(service.get_collection(user.user_id or "", coll_id, with_items=True))


@router.patch("/collections/{coll_id}")
def update_collection(body: CollectionPatchBody, coll_id: str, user: CurrentUser = Depends(require_user)) -> JSONResponse:
    return JSONResponse(
        {"collection": service.update_collection(user.user_id or "", coll_id, body.name, body.description)}
    )


@router.delete("/collections/{coll_id}")
def delete_collection(coll_id: str, user: CurrentUser = Depends(require_user)) -> JSONResponse:
    service.delete_collection(user.user_id or "", coll_id)
    return JSONResponse({"ok": True})


@router.post("/collections/{coll_id}/items")
def add_item(body: ItemBody, coll_id: str, user: CurrentUser = Depends(require_user)) -> JSONResponse:
    """视频入集；重复入集 → 400 提示。"""
    item = service.add_item(user.user_id or "", coll_id, body.content_key, url=body.url, title=body.title)
    return JSONResponse({"item": item}, status_code=201)


@router.delete("/collections/{coll_id}/items/{content_key}")
def remove_item(coll_id: str, content_key: str, user: CurrentUser = Depends(require_user)) -> JSONResponse:
    service.remove_item(user.user_id or "", coll_id, content_key)
    return JSONResponse({"ok": True})


# --------------------------------------------------------------------------- #
# 问答会话
# --------------------------------------------------------------------------- #
@router.get("/qa/sessions")
def list_sessions(
    content_key: str = Query("", description="留空返回全部会话"),
    user: CurrentUser = Depends(require_user),
) -> JSONResponse:
    return JSONResponse({"sessions": service.list_sessions(user.user_id or "", content_key or None)})


@router.post("/qa/sessions")
def create_session(body: SessionBody, user: CurrentUser = Depends(require_user)) -> JSONResponse:
    return JSONResponse(
        {"session": service.create_session(user.user_id or "", body.content_key, body.title or "")},
        status_code=201,
    )


@router.get("/qa/sessions/{session_id}")
def get_session(session_id: str, user: CurrentUser = Depends(require_user)) -> JSONResponse:
    """会话详情（含消息列表，供续聊渲染）。"""
    return JSONResponse(service.get_session(user.user_id or "", session_id))


@router.patch("/qa/sessions/{session_id}")
def rename_session(body: SessionPatchBody, session_id: str, user: CurrentUser = Depends(require_user)) -> JSONResponse:
    return JSONResponse({"session": service.rename_session(user.user_id or "", session_id, body.title)})


@router.delete("/qa/sessions/{session_id}")
def delete_session(session_id: str, user: CurrentUser = Depends(require_user)) -> JSONResponse:
    service.delete_session(user.user_id or "", session_id)
    return JSONResponse({"ok": True})
