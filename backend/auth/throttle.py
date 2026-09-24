"""登录端点的进程内 IP 级限流（阶段A 轻量内置实现）。

目的：在完整 ``ratelimit`` 能力包（阶段A 后续/阶段B）上线前，先给最易被爆破的登录端点
加一道「单 IP 单位时间尝试次数」护栏，抵抗分布式撞库。**账号维度**的失败锁定在
``service.login`` 内实现（register_login_failure + locked_until），二者互补。

实现：进程内滑动窗口（deque 存时间戳），threading.Lock 保护。与项目 singleflight 的
「阶段0 单进程足够、阶段1 换 Redis」演进路径一致——完整限流包上线后本模块由策略表接管。
"""

from __future__ import annotations

import threading
import time
from collections import deque

from backend.auth.config import load_settings
from backend.auth.errors import ThrottledError


class _SlidingWindowLimiter:
    """按 key（此处为 IP）的滑动窗口计数限流器（进程内）。"""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._hits: dict[str, deque[float]] = {}

    def check_and_record(
        self, key: str, *, limit: int, window_sec: int, now: float | None = None
    ) -> None:
        """记录一次命中；若窗口内已超过 limit 则抛 ThrottledError（含 retry_after）。"""
        if limit <= 0:
            return  # limit<=0 视为不限流
        ts = time.monotonic() if now is None else now
        with self._guard:
            bucket = self._hits.get(key)
            if bucket is None:
                bucket = deque()
                self._hits[key] = bucket
            # 丢弃窗口外的旧时间戳
            while bucket and ts - bucket[0] >= window_sec:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(1, int(window_sec - (ts - bucket[0])) + 1)
                raise ThrottledError(retry_after=retry_after)
            bucket.append(ts)

    def reset(self, key: str | None = None) -> None:
        """清空某 key（或全部）的计数，便于测试与人工干预。"""
        with self._guard:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)


# 全局单例：登录限流通道
login_limiter = _SlidingWindowLimiter()


def throttle_login(ip: str) -> None:
    """对一次登录尝试按 IP 限流；超限抛 ThrottledError。"""
    settings = load_settings()
    login_limiter.check_and_record(
        ip or "unknown",
        limit=settings.login_ip_max_per_window,
        window_sec=settings.login_ip_window_seconds,
    )
