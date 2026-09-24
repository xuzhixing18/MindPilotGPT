"""认证与个人资料路由：/api/auth/*（门面 + 会话 Cookie）。

设计取舍（与项目既有风格一致）：路由只调 service 门面，**语义化异常直接冒泡**，由
应用级异常处理器 ``auth_error_handler``（在 main.py 注册）统一映射为 HTTP 状态码与
``{"detail": ...}`` 信封——与既有端点（HTTPException）的前端契约保持一致。

端点分组：
- 开放（无需登录）：register / login / logout / avatar/{user_id}（头像本就是公开信息）
- 需登录（``Depends(require_user)``，**不看灰度开关**）：/me 系列——资料本就属于具体用户，
  匿名访问无意义，故与业务端点的 ``require_user_if_enabled`` 区分对待。
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from backend.auth import avatar, service, throttle
from backend.auth.config import auth_enabled, auth_required, load_settings
from backend.auth.dependencies import CurrentUser, client_ip, require_user
from backend.auth.errors import (
    AccountDisabledError,
    AccountLockedError,
    AuthError,
    InvalidCredentialsError,
    NotAuthenticatedError,
    StorageError,
    ThrottledError,
    UserExistsError,
    ValidationError,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


# --------------------------------------------------------------------------- #
# 请求体模型
# --------------------------------------------------------------------------- #
class RegisterBody(BaseModel):
    """注册：邮箱与手机号**至少填一项**（也可两项都填）。"""

    email: str | None = None
    phone: str | None = None
    password: str
    nickname: str | None = None


class LoginBody(BaseModel):
    """登录：单个输入框——含 ``@`` 视为邮箱，否则按手机号处理。

    ``email`` 字段仅为兼容旧客户端/脚本保留：无 ``identifier`` 时回退取它。
    """

    identifier: str | None = None
    email: str | None = None
    password: str
    remember: bool = False   # 记住我：下发更长有效期的会话 Cookie


class ProfileBody(BaseModel):
    """个人资料：全部可选，只更新**显式提交**的字段（``exclude_unset``）。

    因此「传空串清除」（如 ``{"bio": ""}``）与「不传保持原样」可区分。
    邮箱/手机号/密码不在此列，必须走专用端点（需当前密码确认）。
    """

    nickname: str | None = None
    bio: str | None = None
    gender: str | None = None          # unknown / male / female / other
    birthday: str | None = None        # YYYY-MM-DD 或 YYYY-MM；空串清除
    location: str | None = None
    website: str | None = None         # 空串清除


class PasswordBody(BaseModel):
    """改密：当前密码 + 新密码。"""

    current_password: str
    new_password: str


class EmailBody(BaseModel):
    """换绑邮箱：需当前密码确认。"""

    password: str
    email: str


class PhoneBody(BaseModel):
    """绑定/换绑手机号：需当前密码确认；``phone`` 传空串表示解绑。"""

    password: str
    phone: str


# --------------------------------------------------------------------------- #
# 语义化异常 → HTTP 状态码映射（应用级处理器，兼容既有 {"detail": ...} 契约）
# --------------------------------------------------------------------------- #
_STATUS_BY_ERROR: dict[type, int] = {
    ValidationError: 400,
    UserExistsError: 409,
    InvalidCredentialsError: 401,
    NotAuthenticatedError: 401,
    AccountDisabledError: 403,
    AccountLockedError: 423,
    ThrottledError: 429,
    StorageError: 500,
}


def auth_error_handler(_request: Request, exc: AuthError) -> JSONResponse:
    """把 auth 语义化异常统一转为 JSONResponse（Starlette 按 MRO 匹配，覆盖全部子类）。"""
    status = _STATUS_BY_ERROR.get(type(exc), 400)
    headers = {"Retry-After": str(exc.retry_after)} if isinstance(exc, ThrottledError) else None
    return JSONResponse({"detail": str(exc)}, status_code=status, headers=headers)


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
@router.post("/register")
def register(body: RegisterBody) -> JSONResponse:
    """注册新用户（不自动登录，需再调 /login）。邮箱与手机号至少其一。"""
    user = service.register(
        email=body.email,
        phone=body.phone,
        password=body.password,
        nickname=body.nickname or "",
    )
    return JSONResponse({"user": user}, status_code=201)


@router.post("/login")
def login(body: LoginBody, request: Request) -> JSONResponse:
    """校验凭证并下发会话 Cookie（httpOnly）；remember=true 时有效期更长。"""
    ip = client_ip(request)
    throttle.throttle_login(ip)  # 超限抛 ThrottledError → 429
    user, token, expires_at = service.login(
        body.identifier or body.email or "",
        body.password,
        user_agent=request.headers.get("user-agent", ""),
        ip=ip,
        remember=body.remember,
    )
    settings = load_settings()
    max_age = int((expires_at - datetime.now(timezone.utc)).total_seconds())
    resp = JSONResponse({"user": user, "expires_at": expires_at.isoformat()})
    resp.set_cookie(
        key=settings.cookie_name,
        value=token,
        max_age=max(1, max_age),
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        path=settings.cookie_path,
    )
    return resp


@router.post("/logout")
def logout(request: Request) -> JSONResponse:
    """登出：撤销当前会话并清除 Cookie（幂等，未登录也返回成功）。"""
    settings = load_settings()
    token = request.cookies.get(settings.cookie_name, "")
    service.logout(token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(key=settings.cookie_name, path=settings.cookie_path)
    return resp


@router.get("/me")
def me(user: CurrentUser = Depends(require_user)) -> JSONResponse:
    """返回当前登录用户的完整个人资料；未登录 → 401（由 require_user 抛出、处理器映射）。"""
    return JSONResponse({
        "user": service.get_profile(user.user_id or ""),
        "auth_enabled": auth_enabled(),
        "auth_required": auth_required(),
    })


@router.patch("/me")
def update_me(body: ProfileBody, user: CurrentUser = Depends(require_user)) -> JSONResponse:
    """更新个人资料（昵称/签名/性别/生日/地区/网站），返回最新资料。"""
    profile = service.update_profile(user.user_id or "", body.model_dump(exclude_unset=True))
    return JSONResponse({"user": profile})


@router.post("/me/password")
def change_password(
    body: PasswordBody,
    request: Request,
    user: CurrentUser = Depends(require_user),
) -> JSONResponse:
    """改密：成功后**其他设备的会话全部失效**，当前会话保留（不踢自己）。"""
    settings = load_settings()
    service.change_password(
        user.user_id or "",
        body.current_password,
        body.new_password,
        current_token=request.cookies.get(settings.cookie_name, "") or "",
    )
    return JSONResponse({"ok": True})


@router.post("/me/email")
def change_email(body: EmailBody, user: CurrentUser = Depends(require_user)) -> JSONResponse:
    """换绑邮箱（需当前密码确认）；换绑后 email_verified 归零。"""
    profile = service.change_email(user.user_id or "", body.password, body.email)
    return JSONResponse({"user": profile})


@router.post("/me/phone")
def change_phone(body: PhoneBody, user: CurrentUser = Depends(require_user)) -> JSONResponse:
    """绑定/换绑手机号（需当前密码确认）；``phone`` 传空串表示解绑。"""
    profile = service.change_phone(user.user_id or "", body.password, body.phone)
    return JSONResponse({"user": profile})


# --------------------------------------------------------------------------- #
# 头像
# --------------------------------------------------------------------------- #
_UPLOAD_CHUNK = 64 * 1024


async def _read_upload(file: UploadFile, max_bytes: int) -> bytes:
    """按块读取上传内容，超限即止（避免恶意大文件一次性吃满内存）。"""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ValidationError(
                f"图片过大（上限 {max_bytes // (1024 * 1024)}MB），请压缩后再上传。"
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/me/avatar")
async def upload_avatar(
    file: UploadFile = File(..., description="头像图片（JPG/PNG/GIF/WEBP）"),
    user: CurrentUser = Depends(require_user),
) -> JSONResponse:
    """上传头像：按**魔数**而非 Content-Type 判定真实格式，返回带新版本的 avatar_url。"""
    data = await _read_upload(file, load_settings().avatar_max_bytes)
    return JSONResponse({"user": service.set_avatar(user.user_id or "", data)})


@router.delete("/me/avatar")
def delete_avatar(user: CurrentUser = Depends(require_user)) -> JSONResponse:
    """移除头像（幂等），回退到前端的默认头像。"""
    return JSONResponse({"user": service.remove_avatar(user.user_id or "")})


@router.get("/avatar/{user_id}")
def get_avatar(user_id: str) -> Response:
    """公开读取头像（无需登录）；未设置 → 404，前端回退到首字母/默认图标。

    缓存：URL 带 ``?v=`` 版本参数（每次上传都换），故可长缓存 + immutable，
    客户端无需回源校验即可拿到新图。
    """
    found = avatar.read_avatar(user_id)
    if found is None:
        raise HTTPException(status_code=404, detail="头像不存在。")
    data, mime = found
    return Response(
        content=data,
        media_type=mime,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
