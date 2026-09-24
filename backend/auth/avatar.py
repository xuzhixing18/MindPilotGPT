"""头像存储：本地文件系统 + 魔数校验（不引入 Pillow 等图像库）。

为什么信文件头而不信 ``Content-Type``：请求头由客户端任意伪造。把 HTML / SVG 改名成
``image/png`` 上传、再被浏览器当图片渲染，就是存储型 XSS 的入口。故一律按魔数判定真实
格式，且**只收位图**——SVG 是 XML、可内嵌脚本，明确拒绝。

落盘策略：``{avatar_dir}/{user_id}.{ext}``。换头像即覆盖，并清掉旧扩展名的残留文件，
因此一个用户恒定最多占一个文件，不产生孤儿。``user_id`` 虽取自数据库（uuid4 hex），
仍额外校验字符集，杜绝路径穿越。

缓存：URL 带随机版本参数（``?v=xxxxxxxx``），配合路由侧 ``immutable`` 长缓存——每次
上传都换 URL，客户端无需回源校验即可拿到新图。

阶段1 迁对象存储时只需替换 save / read / delete 三个函数；service 与 router 的契约
（``avatar_url`` 是可直接塞进 ``<img src>`` 的相对地址）保持不变。
"""

from __future__ import annotations

import logging
import re
import secrets
from pathlib import Path

from backend.auth.config import load_settings
from backend.auth.errors import StorageError, ValidationError

log = logging.getLogger(__name__)

# user_id 恒为 uuid4 hex；此校验纯防御（挡路径穿越），正常流程不会触发
_USER_ID_RE = re.compile(r"^[0-9a-fA-F]{1,64}$")
_EXTENSIONS = ("jpg", "png", "gif", "webp")

# 位图魔数（顺序即匹配顺序）
_SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
)


def sniff_image(data: bytes) -> tuple[str, str] | None:
    """按文件头识别真实格式 → ``(ext, mime)``；非受支持位图 → ``None``。"""
    for magic, ext, mime in _SIGNATURES:
        if data.startswith(magic):
            return ext, mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":  # WEBP 是 RIFF 容器
        return "webp", "image/webp"
    return None


def _check_id(user_id: str) -> str:
    uid = (user_id or "").strip()
    if not _USER_ID_RE.match(uid):
        raise ValidationError("用户标识非法。")
    return uid


def avatar_dir() -> Path:
    """头像存储目录（AVATAR_DIR，默认 ``data/avatars``）。"""
    return load_settings().avatar_dir


def avatar_url(user_id: str, version: str) -> str:
    """可直接用于 ``<img src>`` 的相对地址（带版本参数以支持长缓存）。"""
    return f"/api/auth/avatar/{_check_id(user_id)}?v={version}"


def find_avatar(user_id: str) -> Path | None:
    """返回已存在的头像文件（受支持扩展名之一），无则 ``None``。"""
    uid = _check_id(user_id)
    base = avatar_dir()
    for ext in _EXTENSIONS:
        path = base / f"{uid}.{ext}"
        if path.is_file():
            return path
    return None


def save_avatar(user_id: str, data: bytes) -> tuple[str, str]:
    """校验并落盘，返回 ``(avatar_url, mime)``。

    校验顺序刻意「先便宜后昂贵」：非空 → 体积 → 魔数，避免为超大文件白做功。
    """
    uid = _check_id(user_id)
    settings = load_settings()
    if not data:
        raise ValidationError("请选择要上传的图片。")
    if len(data) > settings.avatar_max_bytes:
        limit_mb = max(settings.avatar_max_bytes, 0) / (1024 * 1024)
        raise ValidationError(f"图片过大（上限 {limit_mb:.0f}MB），请压缩后再上传。")
    sniffed = sniff_image(data)
    if sniffed is None:
        raise ValidationError("仅支持 JPG / PNG / GIF / WEBP 格式的图片。")
    ext, mime = sniffed

    base = settings.avatar_dir
    try:
        base.mkdir(parents=True, exist_ok=True)
        (base / f"{uid}.{ext}").write_bytes(data)
        # 换格式上传时清掉旧扩展名残留，保证「一人一文件」
        for old in _EXTENSIONS:
            if old != ext:
                stale = base / f"{uid}.{old}"
                if stale.is_file():
                    stale.unlink()
    except OSError as exc:
        log.error("[avatar] 写入失败 user=%s dir=%s: %s", uid, base, exc)
        raise StorageError("头像保存失败，请稍后重试。") from exc
    return avatar_url(uid, secrets.token_hex(4)), mime


def read_avatar(user_id: str) -> tuple[bytes, str] | None:
    """读取头像内容与 MIME；不存在或读失败 → ``None``（路由侧回 404）。"""
    path = find_avatar(user_id)
    if path is None:
        return None
    try:
        data = path.read_bytes()
    except OSError as exc:
        log.error("[avatar] 读取失败 %s: %s", path.name, exc)
        return None
    mime = (sniff_image(data) or ("", "application/octet-stream"))[1]
    return data, mime


def delete_avatar(user_id: str) -> bool:
    """删除头像文件；返回是否确有文件被删（幂等）。"""
    path = find_avatar(user_id)
    if path is None:
        return False
    try:
        path.unlink()
        return True
    except OSError as exc:
        log.error("[avatar] 删除失败 %s: %s", path.name, exc)
        return False
