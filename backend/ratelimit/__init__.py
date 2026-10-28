"""业务限流与并发闸（P0 风控加固，进程内实现）。

门面 + 可插拔：策略表（policies）配置化、算法纯函数可测（algorithms）、计数后端
进程内（阶段1 换 Redis 时业务零改动，与 singleflight 同演进路径）。
IP 级限流挂中间件（鉴权前，抗匿名洪水）；用户级速率与重活并发闸挂路由依赖。
"""

from __future__ import annotations

from backend.ratelimit.algorithms import SlidingWindowLimiter
from backend.ratelimit.concurrency import HeavyGate
from backend.ratelimit.dependencies import check_user_rate, heavy_gate
from backend.ratelimit.middleware import RateLimitMiddleware
from backend.ratelimit.policies import Policy, enabled, get_policy


def reset() -> None:
    """清空进程内计数（应用重载 / 测试隔离用；对在途请求无副作用）。"""
    from backend.ratelimit.dependencies import user_limiter
    from backend.ratelimit.middleware import ip_limiter

    ip_limiter.reset()
    user_limiter.reset()
