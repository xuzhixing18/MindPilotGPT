"""IP 级滑动窗口限流中间件（鉴权前，抗匿名洪水）。

纯 ASGI 实现（不用 BaseHTTPMiddleware）：/api/stream 是长流式响应，纯 ASGI 零中转
开销。只对策略表设了 ip_rpm 的路由生效；/api/auth、/api/health、/api/admin 与
静态资源不在表内，天然跳过。429 直接构造 JSON 响应（不经异常处理链），并注入
X-RateLimit-* 头；放行响应同样附带余量头，便于前端/调用方观测。
"""

from __future__ import annotations

from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.auth.dependencies import client_ip
from backend.ratelimit.algorithms import SlidingWindowLimiter
from backend.ratelimit.policies import WINDOW_SEC, enabled, get_policy

# 进程内计数（多 worker 各自计数，可接受降级；阶段1 换 Redis 同接口）
ip_limiter = SlidingWindowLimiter()


class RateLimitMiddleware:
    """IP 级滑动窗口限流（鉴权前）。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not enabled():
            await self.app(scope, receive, send)
            return
        # 中间件运行在路由匹配之前，scope["route"] 尚未赋值，直接取 scope["path"]。
        path = scope.get("path")
        policy = get_policy(path)
        if policy is None or policy.ip_rpm <= 0:
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        key = f"ip:{client_ip(request)}:{path}"
        allowed, remaining, retry_after = ip_limiter.allow(
            key, limit=policy.ip_rpm, window_sec=WINDOW_SEC,
        )
        if not allowed:
            response = JSONResponse(
                status_code=429,
                content={"detail": "请求过于频繁，请稍后再试。"},
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(policy.ip_rpm),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(WINDOW_SEC),
                },
            )
            await response(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-RateLimit-Limit"] = str(policy.ip_rpm)
                headers["X-RateLimit-Remaining"] = str(remaining)
                headers["X-RateLimit-Reset"] = str(WINDOW_SEC)
                # 用户级余量头：check_user_rate 依赖写入 request.state（依赖在端点层
                # 执行，头在响应流出时统一并入；含用户级 429 响应）
                user_headers = (scope.get("state") or {}).get("ratelimit_user_headers") or {}
                for name, value in user_headers.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)
