"""QR 码 SVG 生成（纯 Python ``qrcode`` 库的 SvgPathImage 工厂，无需 Pillow）。

海报 QR 走「后端 SVG → 前端 data-URI → canvas」管线：SVG 内无外部资源，
drawImage 不污染 canvas（与 core.js downloadSvgPng 既有的结论一致）。
"""

from __future__ import annotations

from backend.share.errors import ValidationError


def svg_for(data: str, box: int = 4) -> str:
    """把字符串编码为 QR SVG 文档字符串；box 为模块像素尺寸。"""
    try:
        import qrcode
        from qrcode.image.svg import SvgPathImage
    except ImportError as exc:  # 依赖缺失明确报错，避免 500 裸栈
        raise ValidationError("服务端未安装 qrcode 依赖，无法生成二维码。") from exc

    factory = qrcode.make(
        data,
        image_factory=SvgPathImage,
        box_size=max(2, min(12, box)),
        border=2,
    )
    raw = factory.to_string()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return raw
