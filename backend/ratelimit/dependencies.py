"""用户级速率与重活并发闸（FastAPI 依赖，P0 风控）。

- check_user_rate：已登录用户在 IP 中间件之上叠加用户级滑动窗口（429 + Retry-After）。
  余量头写入 request.state，由 RateLimitMiddleware 在响应流出时统一并入——业务端点
  大多直接返回 JSONResponse/FileResponse，注入的 Response 参数会被丢弃，不能走
  ``response.headers`` 路线。匿名请求已由 IP 中间件覆盖，此处直接放行。
- heavy_gate：重活端点（下载/转写/总结/导图）的每用户 + 全局信号量并发闸，超限 429。
  yield 依赖：端点正常返回或抛异常，FastAPI 都保证执行 finally 释放。

二者复用的 require_user_if_enabled 与 main._AUTH_GATE 同源，FastAPI 按请求缓存
依赖结果（use_cache 默认 true），不会重复解析会话。
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from backend.auth.dependencies import CurrentUser, client_ip, require_user_if_enabled
from backend.ratelimit.algorithms import SlidingWindowLimiter
from backend.ratelimit.concurrency import HeavyGate
from backend.ratelimit.policies import (
    WINDOW_SEC,
    enabled,
    get_policy,
    heavy_global_max,
    heavy_per_user,
)

user_limiter = SlidingWindowLimiter()
heavy_gates = HeavyGate(per_user=heavy_per_user(), global_max=heavy_global_max())


def _route_path(request: Request) -> str | None:
    route = request.scope.get("route")
    return getattr(route, "path", None)


def check_user_rate(
    request: Request,
    user: CurrentUser = Depends(require_user_if_enabled),
) -> None:
    """用户级滑动窗口限流（匿名由 IP 中间件覆盖）；超限抛 429。"""
    if not enabled() or not user.is_authenticated:
        return
    path = _route_path(request)
    policy = get_policy(path)
    if policy is None or policy.user_rpm <= 0:
        return
    key = f"user:{user.user_id}:{path}"
    allowed, remaining, retry_after = user_limiter.allow(
        key, limit=policy.user_rpm, window_sec=WINDOW_SEC,
    )
    # 余量头经 request.state 交中间件并入响应（含 429 响应）
    request.state.ratelimit_user_headers = {
        "X-RateLimit-User-Limit": str(policy.user_rpm),
        "X-RateLimit-User-Remaining": str(remaining),
    }
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="请求过于频繁，请稍后再试。",
            headers={"Retry-After": str(retry_after)},
        )


def heavy_gate(
    request: Request,
    user: CurrentUser = Depends(require_user_if_enabled),
):
    """重活并发闸：非阻塞 acquire，拿不到 429；yield 后必然释放。"""
    if not enabled():
        yield
        return
    path = _route_path(request)
    policy = get_policy(path)
    if policy is None or not policy.heavy:
        yield
        return
    key = f"user:{user.user_id}" if user.is_authenticated else f"ip:{client_ip(request)}"
    if not heavy_gates.acquire(key):
        raise HTTPException(
            status_code=429,
            detail="服务器繁忙，请稍后重试。",
            headers={"Retry-After": "5"},
        )
    try:
        yield
    finally:
        heavy_gates.release(key)
