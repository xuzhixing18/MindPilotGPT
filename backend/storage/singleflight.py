"""进程内并发去重（single-flight）。

同一 key 的并发调用只真正执行一次 ``fn``，其余请求等待并复用其结果——避免用户
狂点按钮或同时点「查看字幕 + AI 总结」时，对同一视频重复触发昂贵的抓取/ASR/LLM。

调用方在传入的 ``fn`` 内部自行做「双重检查」（拿到锁后再查一次缓存），因此本类
只负责按 key 串行化，不关心缓存细节。

阶段0 单进程足够；阶段1 多进程/多实例部署时，换成 Redis 分布式锁即可（接口不变）。
"""

from __future__ import annotations

import threading
from typing import Callable, TypeVar

T = TypeVar("T")


class SingleFlight:
    """按 key 串行化并发调用，相同 key 同一时刻只跑一个 fn。"""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._waiters: dict[str, int] = {}

    def _acquire_slot(self, key: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
                self._waiters[key] = 0
            self._waiters[key] += 1
            return lock

    def _release_slot(self, key: str) -> None:
        with self._guard:
            remaining = self._waiters.get(key, 0) - 1
            if remaining > 0:
                self._waiters[key] = remaining
            else:
                # 无人等待：清理，避免注册表无限增长
                self._waiters.pop(key, None)
                self._locks.pop(key, None)

    def run(self, key: str, fn: Callable[[], T]) -> T:
        """串行执行 fn(key)：相同 key 的并发调用依次进入，fn 内应自行做缓存双重检查。"""
        lock = self._acquire_slot(key)
        try:
            with lock:
                return fn()
        finally:
            self._release_slot(key)
