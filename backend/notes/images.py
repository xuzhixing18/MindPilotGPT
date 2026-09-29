"""笔记图片落盘：魔数校验 + 文件名即 id（防路径穿越）。

复用 auth/avatar.py 确立的规范：按**魔数**判定真实格式（不信 Content-Type），
只收位图、明确拒绝 SVG（可内嵌脚本的 XML）；文件名恒为 uuid4 hex，写/读前
正则校验，杜绝路径穿越。阶段1 迁对象存储时只需替换 save/read/delete 三函数。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from backend.notes import config
from backend.notes.errors import StorageError, ValidationError

log = logging.getLogger(__name__)

# image id 恒为 uuid4 hex；校验纯防御（挡路径穿越），正常流程不会触发
_IMAGE_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# 位图魔数（顺序即匹配顺序）；与 auth.avatar.sniff_image 同源，独立成份避免跨包私有依赖
_SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
)


def sniff_image(data: bytes) -> tuple[str, str] | None:
    """按文件头识别真实格式 → (ext, mime)；非受支持位图（含 SVG）→ None。"""
    for magic, ext, mime in _SIGNATURES:
        if data.startswith(magic):
            return ext, mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":  # WEBP 是 RIFF 容器
        return "webp", "image/webp"
    return None


def check_image_id(image_id: str) -> str:
    """校验图片 id 为 uuid4 hex；不合法抛 ValidationError（防路径穿越）。"""
    iid = (image_id or "").strip().lower()
    if not _IMAGE_ID_RE.match(iid):
        raise ValidationError("图片标识非法。")
    return iid


def _path(image_id: str, ext: str) -> Path:
    return config.note_image_dir() / f"{check_image_id(image_id)}.{ext}"


def find_image(image_id: str) -> Path | None:
    """返回已存在的图片文件（受支持扩展名之一），无则 None。"""
    check_image_id(image_id)
    base = config.note_image_dir()
    for _, ext, _ in _SIGNATURES:
        p = base / f"{image_id}.{ext}"
        if p.is_file():
            return p
    p = base / f"{image_id}.webp"
    return p if p.is_file() else None


def save_image(image_id: str, data: bytes) -> tuple[str, str]:
    """校验（体积→魔数）并落盘，返回 (ext, mime)。文件名即 id，同 id 重写即覆盖。"""
    if not data:
        raise ValidationError("图片内容为空。")
    limit = config.note_image_max_bytes()
    if len(data) > limit:
        raise ValidationError(f"图片过大（上限 {limit // (1024 * 1024)}MB），请压缩后再上传。")
    sniffed = sniff_image(data)
    if sniffed is None:
        raise ValidationError("仅支持 JPG / PNG / GIF / WEBP 格式的图片。")
    ext, mime = sniffed
    try:
        base = config.note_image_dir()
        base.mkdir(parents=True, exist_ok=True)
        _path(image_id, ext).write_bytes(data)
        # 同 id 换格式重写时清掉旧扩展名残留，保证「一图一文件」
        for other in ("jpg", "png", "gif", "webp"):
            if other != ext:
                stale = base / f"{image_id}.{other}"
                if stale.is_file():
                    stale.unlink()
    except OSError as exc:
        log.error("[notes] 图片写入失败 id=%s: %s", image_id, exc)
        raise StorageError("图片保存失败，请稍后重试。") from exc
    return ext, mime


def read_image(image_id: str) -> tuple[bytes, str] | None:
    """读取图片内容与 MIME；不存在 → None（路由侧回 404）。"""
    path = find_image(image_id)
    if path is None:
        return None
    try:
        data = path.read_bytes()
    except OSError as exc:
        log.error("[notes] 图片读取失败 %s: %s", path.name, exc)
        return None
    mime = (sniff_image(data) or ("", "application/octet-stream"))[1]
    return data, mime


def delete_image_file(image_id: str) -> bool:
    """删除图片文件（幂等）；返回是否确有文件被删。"""
    path = find_image(image_id)
    if path is None:
        return False
    try:
        path.unlink()
        return True
    except OSError as exc:
        log.error("[notes] 图片删除失败 %s: %s", path.name, exc)
        return False
