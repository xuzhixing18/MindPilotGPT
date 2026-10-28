"""滑动窗口限流纯算法（进程内，假时钟可测）。

与 auth/throttle.py 的登录限流器同构，抽出为通用件供 IP / 用户两级复用：
计数存进程内（阶段0 单机足够；阶段1 换 Redis 计数后端时接口不变，与
singleflight 同演进路径）。
"""

from __future__ import annotations

import threading
import time
from collections import deque


class SlidingWindowLimiter:
    """按 key 的滑动窗口计数：窗口内第 limit+1 次命中被拒并给出重试秒数。

    ``now`` 参数支持假时钟注入（离线单测窗口边界）；limit<=0 视为不限流。
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._hits: dict[str, deque[float]] = {}

    def allow(
        self, key: str, *, limit: int, window_sec: int, now: float | None = None
    ) -> tuple[bool, int, int]:
        """返回 (allowed, remaining, retry_after)。放行时记录一次命中。"""
        if limit <= 0:
            return True, 0, 0
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
                return False, 0, retry_after
            bucket.append(ts)
            return True, limit - len(bucket), 0

    def reset(self, key: str | None = None) -> None:
        """清空某 key（或全部）的计数，便于测试与人工干预。"""
        with self._guard:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)
