"""重活并发闸：每用户信号量 + 全局信号量（护线程池，P0 风控）。

非阻塞 acquire：拿不到即拒绝（依赖层翻译为 429），不排队——排队需要任务队列
（阶段B）。全局闸防整池被重活占满（下载/转写/LLM 是同步阻塞任务）。
"""

from __future__ import annotations

import threading


class HeavyGate:
    """两级信号量：全局上限 + 每 key（用户/IP）上限。"""

    def __init__(self, *, per_user: int, global_max: int) -> None:
        self._per_user = max(1, per_user)
        self._global = threading.Semaphore(max(1, global_max))
        self._guard = threading.Lock()
        self._slots: dict[str, threading.Semaphore] = {}

    def _slot(self, key: str) -> threading.Semaphore:
        with self._guard:
            sem = self._slots.get(key)
            if sem is None:
                sem = self._slots[key] = threading.Semaphore(self._per_user)
            return sem

    def acquire(self, key: str) -> bool:
        """非阻塞占用两级名额；失败不残留任何计数。"""
        if not self._global.acquire(blocking=False):
            return False
        if not self._slot(key).acquire(blocking=False):
            self._global.release()
            return False
        return True

    def release(self, key: str) -> None:
        """释放两级名额（仅对 acquire 成功的调用）。"""
        with self._guard:
            sem = self._slots.get(key)
        if sem is not None:
            sem.release()
        self._global.release()
