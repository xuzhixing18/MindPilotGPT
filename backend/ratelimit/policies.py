"""限流策略注册表（P0 风控加固）：新增端点只加一行（类比 downloader.registry）。

数值为 P0 实测口径：比《商业化落地设计方案》§六 初稿宽松——批量下载、流播 Range
请求、多标签页并发是既有产品的正常流量，P1 接入套餐权益后按 entitlements 配额收紧。
- 匿名走 IP 级（中间件）；登录用户叠加用户级（依赖）。
- /api/me/ 前缀只设用户级：私有端点必须登录，IP 级无意义。
- /api/stream 不挂并发闸：<video> 播放会并发多个 Range 请求，信号量会误伤正常播放。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

WINDOW_SEC = 60  # 滑动窗口长度（秒）


@dataclass(frozen=True)
class Policy:
    """单条限流策略。route 为精确路径，或以 "/" 结尾表示前缀匹配。"""

    route: str
    ip_rpm: int = 0       # 0 = 不设 IP 级
    user_rpm: int = 0     # 0 = 不设用户级
    heavy: bool = False   # 是否挂重活并发闸


# 精确路由表（顺序无关）
POLICIES: dict[str, Policy] = {p.route: p for p in (
    Policy("/api/info", ip_rpm=60, user_rpm=120),                  # 解析有 DB 缓存，批量解析常见
    Policy("/api/download", ip_rpm=20, user_rpm=20, heavy=True),   # 重（字节）；套餐配额 P1 接
    Policy("/api/stream", ip_rpm=120, user_rpm=60),                # 单次播放会发多个 Range 请求
    Policy("/api/transcribe", ip_rpm=10, user_rpm=30, heavy=True),
    Policy("/api/summarize", ip_rpm=6, user_rpm=10, heavy=True),
    Policy("/api/mindmap", ip_rpm=6, user_rpm=10, heavy=True),
    Policy("/api/mindmap/xmind", ip_rpm=6, user_rpm=10, heavy=True),
    Policy("/api/qa", ip_rpm=10, user_rpm=20),
    Policy("/api/comments", ip_rpm=20, user_rpm=40),
)}
# 前缀策略表（按声明顺序匹配）
PREFIX_POLICIES: tuple[Policy, ...] = (
    Policy("/api/me/", user_rpm=240),   # 私有端点廉价 DB 操作；仅用户级
)


def get_policy(path: str | None) -> Policy | None:
    """查策略：精确命中优先，其次前缀匹配；未覆盖路径不限流。"""
    if not path:
        return None
    policy = POLICIES.get(path)
    if policy is not None:
        return policy
    for pref in PREFIX_POLICIES:
        if path.startswith(pref.route):
            return pref
    return None


def enabled() -> bool:
    """业务限流总开关（默认 true；false 仅保留既有登录/分享局部限流）。"""
    return os.getenv("RATELIMIT_ENABLED", "true").strip().lower() not in ("0", "false", "no")


def heavy_per_user() -> int:
    """每用户重活并发上限。"""
    return _int_env("RATELIMIT_HEAVY_PER_USER", 2)


def heavy_global_max() -> int:
    """全站重活并发上限（护线程池）。"""
    return _int_env("RATELIMIT_HEAVY_GLOBAL", 16)


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, default)))
    except ValueError:
        return default
