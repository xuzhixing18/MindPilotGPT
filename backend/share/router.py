"""share 路由：私有 /api/me/shares/* + 公开 /s/{code}、/api/share/*。

公开端点**不挂门禁**（分享本体即公开物，与 /api/health 同列），但挂进程内滑动
窗口限流；私有端点一律 ``Depends(require_user)``（创建分享是登录态动作）。
``/s/{code}`` 用真实路径服务端渲染（爬虫/微信爬虫直读 og），显式路由优先于
main.py 末尾的 StaticFiles mount。
"""

from __future__ import annotations

from urllib.parse import urlsplit

import requests
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from backend.auth.dependencies import CurrentUser, client_ip, require_user
from backend.share import config, landing, qr, service
from backend.share.errors import (
    GoneError,
    NotFoundError,
    ShareError,
    StorageError,
    ThrottledError,
    ValidationError,
)

router = APIRouter(tags=["share"])


# --------------------------------------------------------------------------- #
# 语义化异常 → HTTP
# --------------------------------------------------------------------------- #
_STATUS_BY_ERROR: dict[type, int] = {
    NotFoundError: 404,
    GoneError: 410,
    ValidationError: 400,
    ThrottledError: 429,
    StorageError: 500,
}


def share_error_handler(_request: Request, exc: ShareError) -> JSONResponse:
    """share 语义化异常统一转 JSONResponse（Starlette 按 MRO 匹配子类）。"""
    headers = {"Retry-After": str(exc.retry_after)} if isinstance(exc, ThrottledError) else None
    return JSONResponse({"detail": str(exc)}, status_code=_STATUS_BY_ERROR.get(type(exc), 400), headers=headers)


def _base_url(request: Request) -> str:
    """短链域名事实源：PUBLIC_BASE_URL 优先，未配置回退请求 base_url（去尾斜杠）。"""
    return config.public_base_url() or str(request.base_url).rstrip("/")


def _rate_limit(request: Request) -> None:
    service.check_rate(client_ip(request))


# --------------------------------------------------------------------------- #
# 私有：创建 / 列表 / 撤销
# --------------------------------------------------------------------------- #
class ShareBody(BaseModel):
    """创建分享：snapshot 为前端「所见即所享」上报，服务端按上限消毒不信任。"""

    content_key: str
    kind: str = "video"
    url: str = ""
    ref_id: str = ""
    force_new: bool = False
    snapshot: dict = {}


@router.post("/api/me/shares")
def create_share(body: ShareBody, request: Request, user: CurrentUser = Depends(require_user)) -> JSONResponse:
    """创建分享（幂等：同载体复用旧码）；201 新建 / 200 复用。"""
    share, created = service.create_share(
        user.user_id or "", body.content_key,
        kind=body.kind, url=body.url, ref_id=body.ref_id,
        snapshot=body.snapshot, force_new=body.force_new,
    )
    return JSONResponse({"share": service.public_dict(share, _base_url(request))},
                        status_code=201 if created else 200)


@router.get("/api/me/shares")
def list_shares(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: CurrentUser = Depends(require_user),
) -> JSONResponse:
    """我的分享列表（管理用，含已撤销行）。"""
    return JSONResponse(service.list_shares(user.user_id or "", limit=limit, offset=offset))


@router.delete("/api/me/shares/{code}")
def revoke_share(code: str, user: CurrentUser = Depends(require_user)) -> JSONResponse:
    """撤销分享：落地页与公开 API 随即 410。"""
    service.revoke_share(user.user_id or "", code)
    return JSONResponse({"ok": True})


# --------------------------------------------------------------------------- #
# 公开：落地页 / 快照 / 封面代理 / QR
# --------------------------------------------------------------------------- #
@router.get("/s/{code}")
def landing_page(code: str, request: Request, _: None = Depends(_rate_limit)) -> HTMLResponse:
    """服务端渲染落地页：og meta + 首图兜底 + 快照正文 + CTA（匿名可读）。"""
    try:
        pub = service.get_public(code, _base_url(request))
    except GoneError:
        return HTMLResponse(
            landing.render_gone("该分享已被创建者撤销", "内容不再对外展示。去 MindPilot 解析你自己的视频吧。"),
            status_code=410,
        )
    except NotFoundError:
        return HTMLResponse(
            landing.render_gone("分享不存在或已失效", "链接可能有误，或创建者已将其撤销。"),
            status_code=404,
        )
    # 浏览计数：匿名访客只访问落地页（纯 HTML 无 JS），若只在 JSON 端点计数则
    # view_count 永远为 0；IP 窗口去重在 register_view 内部，刷新不重复计。
    service.register_view(code, client_ip(request))
    return HTMLResponse(landing.render(pub, _base_url(request)))


# 注意：/api/share/qr 必须先于 /api/share/{code} 注册——Starlette 按注册顺序匹配，
# 否则 "qr" 会被 {code} 吞掉落入公开快照端点（404）。
@router.get("/api/share/qr")
def qr_svg(
    request: Request,
    data: str = Query(..., min_length=1),
    box: int = Query(4, ge=2, le=12),
    _: None = Depends(_rate_limit),
) -> Response:
    """QR SVG：只编码本站分享链接（防成为任意二维码生成器/钓鱼投毒渠道）。"""
    base = _base_url(request)
    if len(data) > config.qr_max_len():
        raise ValidationError("二维码内容过长。")
    if not data.startswith(f"{base}/"):
        raise ValidationError("只能编码本站分享链接。")
    return Response(
        content=qr.svg_for(data, box),
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/api/share/{code}")
def public_snapshot(code: str, request: Request, _: None = Depends(_rate_limit)) -> JSONResponse:
    """公开 JSON 快照（字段白名单，无 user_id）；顺带 IP 去重浏览计数。"""
    pub = service.get_public(code, _base_url(request))
    service.register_view(code, client_ip(request))
    return JSONResponse(pub)


@router.get("/api/share/{code}/thumb")
def thumb_proxy(code: str, request: Request, _: None = Depends(_rate_limit)) -> Response:
    """封面同源代理：URL 只来自 shares 快照（端点无 url 入参 → SSRF 面为零）。

    同源化解决海报 canvas 跨域污染；content-type 与体积双校验防缓存投毒回传。
    """
    target = service.thumb_target(code)
    if not target:
        raise NotFoundError("该分享没有封面。")
    parts = urlsplit(target)
    if parts.scheme not in ("http", "https"):
        raise NotFoundError("封面地址不合法。")
    try:
        with requests.get(target, timeout=(3, 8), stream=True) as upstream:
            if not upstream.ok:
                raise NotFoundError("封面拉取失败。")
            mime = (upstream.headers.get("content-type") or "").split(";")[0].strip()
            if not mime.startswith("image/"):
                raise NotFoundError("封面类型不合法。")
            chunks: list[bytes] = []
            total = 0
            for chunk in upstream.iter_content(64 * 1024):
                total += len(chunk)
                if total > config.thumb_max_bytes():
                    raise NotFoundError("封面过大。")
                chunks.append(chunk)
        data = b"".join(chunks)
    except requests.RequestException as exc:
        raise NotFoundError("封面拉取失败。") from exc
    return Response(content=data, media_type=mime, headers={"Cache-Control": "public, max-age=86400"})
