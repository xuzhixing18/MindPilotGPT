"""随手笔记路由：/api/me/notes/*（笔记 CRUD / 图片 / 抽帧）。

与 library.router 同风格：路由只调 service 门面，语义化异常冒泡由
``notes_error_handler``（main.py 注册）统一映射 HTTP。门禁一律
``Depends(require_user)``——私有资源永远要求登录，不看 AUTH_ENABLED 灰度开关。

抽帧端点的 ``cached_file`` 由 main.py 注入（进程内流播缓存查询回调），notes 包
不反向依赖 main，保持能力包自洽。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from backend.auth.dependencies import CurrentUser, require_user
from backend.notes import service
from backend.notes.errors import (
    CapExceededError,
    ConflictError,
    NotesError,
    NotFoundError,
    StorageError,
    ValidationError,
)
router = APIRouter(prefix="/api/me/notes", tags=["notes"])

# main.py 注入：normalized_url → 流播缓存文件路径（未命中返回 None）
_stream_cache_lookup: Callable[[str], Path | None] | None = None


def set_stream_cache_lookup(fn: Callable[[str], Path | None]) -> None:
    """注册流播缓存查询回调（main.py 启动时调用，注入进程内 _stream_cache）。"""
    global _stream_cache_lookup
    _stream_cache_lookup = fn


# --------------------------------------------------------------------------- #
# 请求体模型
# --------------------------------------------------------------------------- #
class NoteBody(BaseModel):
    """新建笔记：content_key 为视频身份键，url/title 为快照。"""

    content_key: str
    url: str = ""
    title: str = ""
    body: str = ""
    tag: str = ""
    source_type: str = "manual"
    source_ref: str = ""


class NotePatchBody(BaseModel):
    """改笔记（自动保存主通道）：只更新显式提交字段；base_updated_at 为并发基线。"""

    body: str | None = None
    title: str | None = None
    tag: str | None = None
    starred: bool | None = None
    base_updated_at: str | None = None


class FrameBody(BaseModel):
    """后端抽帧：B 站/YouTube 等跨域 iframe 平台的截图通道。"""

    url: str
    t: float = 0


# --------------------------------------------------------------------------- #
# 语义化异常 → HTTP
# --------------------------------------------------------------------------- #
_STATUS_BY_ERROR: dict[type, int] = {
    NotFoundError: 404,
    ValidationError: 400,
    CapExceededError: 400,
    ConflictError: 409,
    StorageError: 500,
}


def notes_error_handler(_request: Request, exc: NotesError) -> JSONResponse:
    """notes 语义化异常统一转 JSONResponse（Starlette 按 MRO 匹配子类）。

    409 额外携带服务端当前版本（server 字段），供前端冲突二选一。
    """
    payload: dict = {"detail": str(exc)}
    if isinstance(exc, ConflictError):
        payload["server"] = exc.server
    return JSONResponse(payload, status_code=_STATUS_BY_ERROR.get(type(exc), 400))


# --------------------------------------------------------------------------- #
# 笔记 CRUD
# --------------------------------------------------------------------------- #
@router.get("")
def list_notes(
    content_key: str = Query("", description="视频身份键；留空 = 笔记库全量"),
    q: str = Query("", description="正文模糊搜索"),
    tag: str = Query("", description="语义标签过滤：key/question/idea"),
    starred: bool = Query(False, description="仅看收藏"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: CurrentUser = Depends(require_user),
) -> JSONResponse:
    """笔记列表（updated_at 倒序）+ 总数。"""
    return JSONResponse(service.list_notes(
        user.user_id or "", content_key=content_key, q=q, tag=tag,
        starred=starred, limit=limit, offset=offset,
    ))


@router.post("", status_code=201)
def create_note(body: NoteBody, user: CurrentUser = Depends(require_user)) -> JSONResponse:
    """新建笔记（正文截断不报错，返回 truncated 供前端提示）。"""
    note = service.create_note(
        user.user_id or "", body.content_key, body.body or "",
        url=body.url or "", title=body.title or "", tag=body.tag or "",
        source_type=body.source_type, source_ref=body.source_ref,
    )
    return JSONResponse({"note": note}, status_code=201)


@router.get("/{note_id}")
def get_note(note_id: str, user: CurrentUser = Depends(require_user)) -> JSONResponse:
    """笔记详情（含图片元数据）；不存在或不属于本人 → 404。"""
    return JSONResponse({"note": service.get_note(user.user_id or "", note_id)})


@router.patch("/{note_id}")
def update_note(body: NotePatchBody, note_id: str, user: CurrentUser = Depends(require_user)) -> JSONResponse:
    """自动保存主通道：乐观并发检测（base_updated_at），冲突 → 409 + 服务端版本。"""
    note = service.update_note(
        user.user_id or "", note_id,
        body=body.body, title=body.title, tag=body.tag, starred=body.starred,
        base_updated_at=body.base_updated_at,
    )
    return JSONResponse({"note": note})


@router.delete("/{note_id}")
def delete_note(note_id: str, user: CurrentUser = Depends(require_user)) -> JSONResponse:
    """删笔记（级联删除其图片行与磁盘文件）。"""
    service.delete_note(user.user_id or "", note_id)
    return JSONResponse({"ok": True})


# --------------------------------------------------------------------------- #
# 图片
# --------------------------------------------------------------------------- #
@router.post("/images", status_code=201)
async def upload_image(
    file: UploadFile = File(..., description="截图或贴图（JPG/PNG/GIF/WEBP）"),
    content_key: str = Form(""),
    note_id: str = Form(""),
    t: str = Form(""),
    user: CurrentUser = Depends(require_user),
) -> JSONResponse:
    """传图（截图/贴图共用）：魔数校验、独立落盘；note_id 可空（新建未保存时贴图）。"""
    chunks: list[bytes] = []
    total = 0
    limit = service.config.note_image_max_bytes()
    while True:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ValidationError(f"图片过大（上限 {limit // (1024 * 1024)}MB），请压缩后再上传。")
        chunks.append(chunk)
    data = b"".join(chunks)

    t_val: float | None = None
    if (t or "").strip():
        try:
            t_val = float(t)
        except ValueError:
            raise ValidationError("截图时间点不合法。")

    image = service.upload_image(
        user.user_id or "", data, content_key, t=t_val, note_id=(note_id or "").strip() or None,
    )
    return JSONResponse({"image": image}, status_code=201)


@router.get("/images/{image_id}")
def get_image(image_id: str, user: CurrentUser = Depends(require_user)) -> Response:
    """读取笔记图片（私有资源，需鉴权）。id 即内容指纹，可长缓存 immutable。"""
    found = service.get_image_content(user.user_id or "", image_id)
    if found is None:
        raise HTTPException(status_code=404, detail="图片不存在。")
    data, mime = found
    return Response(
        content=data,
        media_type=mime,
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@router.delete("/images/{image_id}")
def delete_image(image_id: str, user: CurrentUser = Depends(require_user)) -> JSONResponse:
    """删图（编辑器删图时调用）。"""
    service.delete_image(user.user_id or "", image_id)
    return JSONResponse({"ok": True})


# --------------------------------------------------------------------------- #
# 抽帧（B 站 / YouTube 等跨域平台的后端截图通道）
# --------------------------------------------------------------------------- #
@router.post("/frames", status_code=201)
def capture_frame(body: FrameBody, user: CurrentUser = Depends(require_user)) -> JSONResponse:
    """后端抽帧：本地流播缓存 → 远程直连抽帧（只拉 t 附近片段，不下整片）→ 封面兜底。"""
    from backend.notes import frames

    cached: Path | None = None
    if _stream_cache_lookup is not None:
        try:
            cached = _stream_cache_lookup(body.url)
        except Exception:  # noqa: BLE001 缓存查询失败不影响主流程
            cached = None
    shot = frames.capture(body.url, body.t, cached)
    image = service.upload_image(user.user_id or "", shot["data"], _key_of(body.url), t=body.t)
    return JSONResponse({"image": image, "frame_type": shot["kind"]}, status_code=201)


def _key_of(url: str) -> str:
    """抽帧图片归属的视频身份键（同规则：transcript_key）。"""
    from backend.storage import transcript_key

    return transcript_key(url)
