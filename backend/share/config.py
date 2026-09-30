"""share 配置：短链 / 限流 / 快照上限（env 驱动，与 notes/config.py 同范式）。

``PUBLIC_BASE_URL`` 是短链与海报 QR 的域名事实源：反代 / https 部署必须配置，
否则 QR 会编码成请求看到的内网地址（如 http://127.0.0.1:8010）。
"""

from __future__ import annotations

import os

# 分享载体（与前端 TAB 一一对应 + video 整视频载体 + bundle 多 Tab 聚合载体）
KINDS = ("video", "summary", "transcript", "mindmap", "comments", "qa", "notes")
BUNDLE_KIND = "bundle"          # 一个链接聚合多个 Tab（勾选变化同码更新）
ALL_KINDS = KINDS + (BUNDLE_KIND,)

# 快照截断上限（服务端消毒，客户端上报不可信）
EXCERPT_MAX = 500
BULLET_MAX, BULLETS_MAX = 200, 3
TITLE_MAX = 200
NODE_MAX, NODES_MAX = 60, 8
CM_AUTHOR_MAX, CM_TEXT_MAX, CMS_MAX = 40, 200, 3
QA_Q_MAX, QA_A_MAX, QAS_MAX = 200, 300, 3
MODEL_LABEL_MAX = 64


def _int(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or str(default)).strip())
    except ValueError:
        return default


def public_base_url() -> str:
    """短链域名事实源（env 优先，去尾斜杠）；未配置时由 router 按请求 base_url 回退。"""
    return (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")


def share_code_len() -> int:
    """短链码长度（默认 10：base62^10 ≈ 8e17，防枚举）。"""
    return max(6, _int("SHARE_CODE_LEN", 10))


def rate_window_seconds() -> int:
    """公开端点限流窗口（默认 60s）。"""
    return _int("SHARE_RATE_WINDOW", 60)


def rate_max_per_window() -> int:
    """窗口内单 IP 请求上限（默认 120：落地页一次浏览约产生 3 个公开请求）。"""
    return _int("SHARE_RATE_MAX", 120)


def view_dedup_seconds() -> int:
    """浏览计数 IP 去重窗口（默认 600s）。"""
    return _int("SHARE_VIEW_DEDUP", 600)


def thumb_max_bytes() -> int:
    """封面代理回传体积上限（默认 2MB）。"""
    return _int("SHARE_THUMB_MAX_BYTES", 2 * 1024 * 1024)


def qr_max_len() -> int:
    """QR 编码内容长度上限（默认 512 字符）。"""
    return _int("SHARE_QR_MAX_LEN", 512)
