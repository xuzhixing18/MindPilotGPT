"""P0 风控限流离线测试（纯算法单测 + TestClient 全链路，不触外网）。

覆盖：
- policies：策略表精确/前缀命中，未覆盖路径不限流
- algorithms：滑动窗口计数/拒绝/窗口滑过/重置
- concurrency：重活闸每用户 + 全局信号量，acquire 失败无残留
- 中间件（HTTP）：IP 超限 429 + X-RateLimit-* 头 + Retry-After，detail 为字符串
  （前端 core.js 读 data.detail 的契约）
- 用户级（HTTP）：登录后叠加用户级限流，X-RateLimit-User-* 头，超限 429
- SSRF 收口（HTTP）：/api/info 内网链接 400，且先于缓存读取（缓存毒化也拦得住）
- 500 脱敏（HTTP）：底层异常细节（含探针串）不出现在响应里
- CORS：白名单内 Origin 预检放行、白名单外拒绝；_cors_allow_origins 推导链
- 总开关：RATELIMIT_ENABLED=false 时中间件/依赖全部跳过

运行：python tests/test_ratelimit.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 关键：db.py 在「导入时」即按 DATABASE_URL 建 engine，必须在导入任何 backend.* 之前设置临时库。
_TMP_DIR = Path(tempfile.mkdtemp(prefix="mp_rl_test_"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP_DIR / 'test_rl.db').as_posix()}"
os.environ["AUTH_ENABLED"] = "false"               # 默认匿名放行，专注限流验证
os.environ["AUTH_ALLOW_ANONYMOUS"] = "true"
os.environ["LOGIN_IP_MAX_PER_WINDOW"] = "1000"     # 功能测试默认不受登录限流干扰
os.environ["CORS_ALLOW_ORIGINS"] = "https://allowed.example.com"   # CORS 白名单测试钉死
os.environ["RATELIMIT_ENABLED"] = "true"

from fastapi.testclient import TestClient  # noqa: E402

from backend import downloader, ratelimit, storage  # noqa: E402
from backend.main import _cors_allow_origins, app  # noqa: E402
from backend.ratelimit.algorithms import SlidingWindowLimiter  # noqa: E402
from backend.ratelimit.concurrency import HeavyGate  # noqa: E402
from backend.ratelimit.policies import POLICIES, Policy, get_policy  # noqa: E402

client = TestClient(app)


def test_policy_lookup():
    info = get_policy("/api/info")
    assert info is not None and info.ip_rpm == 60 and info.user_rpm == 120 and not info.heavy
    dl = get_policy("/api/download")
    assert dl is not None and dl.ip_rpm == 20 and dl.heavy
    me = get_policy("/api/me/notes/123")
    assert me is not None and me.user_rpm == 240 and me.ip_rpm == 0
    assert get_policy("/api/health") is None          # 健康检查不限流
    assert get_policy("/api/auth/login") is None      # 认证端点走既有局部限流
    assert get_policy("/api/info/extra") is None      # 精确不命中也不落前缀
    assert get_policy(None) is None
    # heavy 端点齐备
    for path in ("/api/download", "/api/transcribe", "/api/summarize",
                 "/api/mindmap", "/api/mindmap/xmind"):
        p = get_policy(path)
        assert p is not None and p.heavy, path
    assert not get_policy("/api/stream").heavy        # Range 并发不挂闸
    print("[ratelimit] policy lookup ok")


def test_sliding_window():
    limiter = SlidingWindowLimiter()
    t = 1000.0
    for i in range(3):
        ok, remaining, _ = limiter.allow("k", limit=3, window_sec=60, now=t + i)
        assert ok and remaining == 2 - i
    ok, remaining, retry = limiter.allow("k", limit=3, window_sec=60, now=t + 5)
    assert not ok and remaining == 0 and retry >= 1
    # 最早时间戳滑出窗口后可再次放行
    ok, _, _ = limiter.allow("k", limit=3, window_sec=60, now=t + 60)
    assert ok
    limiter.reset()
    assert limiter.allow("k", limit=3, window_sec=60, now=t + 100)[0]
    assert limiter.allow("k", limit=0, window_sec=60, now=t + 101)[0]  # limit<=0 不限流
    print("[ratelimit] sliding window algorithm ok")


def test_heavy_gate():
    gate = HeavyGate(per_user=2, global_max=2)
    assert gate.acquire("a") and gate.acquire("a")
    assert not gate.acquire("a")          # 每用户上限
    assert not gate.acquire("b")          # 全局上限
    gate.release("a")
    assert gate.acquire("b")
    assert not gate.acquire("a")          # a 仍占 2 席
    gate.release("a")
    gate.release("a")
    gate.release("b")
    assert gate.acquire("a")              # 全部释放后可再进
    print("[ratelimit] heavy gate ok")


def _patch_extract_info():
    """替换 extract_info 返回罐头数据，避免真实 yt-dlp 触网。"""
    original = downloader.extract_info
    downloader.extract_info = lambda url: {
        "title": "offline-video", "thumbnail": "https://img.example.com/x.jpg",
    }
    return original


def test_ip_rate_limit_429():
    ratelimit.reset()
    original = _patch_extract_info()
    old = POLICIES["/api/info"]
    POLICIES["/api/info"] = Policy(route="/api/info", ip_rpm=3, user_rpm=3)
    try:
        for _ in range(3):
            r = client.get("/api/info", params={"url": "https://public.example.com/video/1"})
            assert r.status_code == 200, r.text
            assert r.headers.get("x-ratelimit-limit") == "3"
        r = client.get("/api/info", params={"url": "https://public.example.com/video/1"})
        assert r.status_code == 429, r.text
        body = r.json()
        assert isinstance(body.get("detail"), str) and body["detail"]   # 前端 data.detail 契约
        assert r.headers.get("retry-after")
        assert r.headers.get("x-ratelimit-limit") == "3"
        assert r.headers.get("x-ratelimit-remaining") == "0"
    finally:
        POLICIES["/api/info"] = old
        downloader.extract_info = original
        ratelimit.reset()
    print("[ratelimit] ip-level 429 with headers ok")


def test_user_rate_429():
    ratelimit.reset()
    original = _patch_extract_info()
    old = POLICIES["/api/info"]
    POLICIES["/api/info"] = Policy(route="/api/info", ip_rpm=100, user_rpm=3)
    os.environ["AUTH_ENABLED"] = "true"              # 开启登录态以触发用户级
    try:
        assert client.post("/api/auth/register", json={
            "email": "rl@example.com", "password": "password123", "nickname": "RL",
        }).status_code in (200, 201)
        assert client.post("/api/auth/login", json={
            "identifier": "rl@example.com", "password": "password123",
        }).status_code == 200
        for _ in range(3):
            r = client.get("/api/info", params={"url": "https://public.example.com/video/u"})
            assert r.status_code == 200, r.text
            assert r.headers.get("x-ratelimit-user-limit") == "3"
        r = client.get("/api/info", params={"url": "https://public.example.com/video/u"})
        assert r.status_code == 429, r.text
        assert r.headers.get("retry-after")
        assert "请求过于频繁" in r.json()["detail"]
    finally:
        os.environ["AUTH_ENABLED"] = "false"
        POLICIES["/api/info"] = old
        downloader.extract_info = original
        ratelimit.reset()
    print("[ratelimit] user-level 429 with headers ok")


def test_ssrf_rejected_before_cache():
    """内网链接即使已在缓存中（加固前写入/缓存毒化）也必须 400——校验先于读缓存。"""
    ratelimit.reset()
    url = "http://127.0.0.1/secret"
    storage.repo.put_info(storage.transcript_key(url),
                          {"title": "cached-internal", "thumbnail": "https://img.example.com/x.jpg"},
                          url)
    r = client.get("/api/info", params={"url": url})
    assert r.status_code == 400, r.text
    assert "内网" in r.json()["detail"]
    # 同源 GET 下载同样收口
    r2 = client.get("/api/download", params={"url": "http://169.254.169.254/latest/meta-data/"})
    assert r2.status_code == 400 and "内网" in r2.json()["detail"]
    print("[ratelimit] SSRF guarded before cache ok")


def test_500_desensitized():
    ratelimit.reset()
    original = _patch_extract_info()

    def boom(url):
        raise RuntimeError("internal-secret-9f3a")

    downloader.extract_info = boom
    try:
        r = client.get("/api/info", params={"url": "https://public.example.com/video/2"})
        assert r.status_code == 500, r.text
        body = r.json()
        assert body["detail"] == "解析服务暂时不可用，请稍后重试。"
        assert "internal-secret-9f3a" not in r.text
        assert "RuntimeError" not in r.text
    finally:
        downloader.extract_info = original
    print("[ratelimit] 500 desensitized ok")


def test_cors_whitelist():
    # 白名单内 Origin：预检放行并回显 Origin（预检不经限流计数）
    r = client.options("/api/info", headers={
        "Origin": "https://allowed.example.com",
        "Access-Control-Request-Method": "GET",
    })
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "https://allowed.example.com"
    # 白名单外 Origin：无 ACAO 头
    r2 = client.options("/api/info", headers={
        "Origin": "https://evil.example.com",
        "Access-Control-Request-Method": "GET",
    })
    assert "access-control-allow-origin" not in r2.headers
    print("[ratelimit] CORS whitelist ok")


def test_cors_origin_resolution():
    os.environ["CORS_ALLOW_ORIGINS"] = ""
    os.environ["PUBLIC_BASE_URL"] = "https://pub.example.com/"
    try:
        assert _cors_allow_origins() == ["https://pub.example.com"]
        os.environ["PUBLIC_BASE_URL"] = ""
        assert _cors_allow_origins() == ["*"]           # 双未配置 → 兜底 *
        os.environ["CORS_ALLOW_ORIGINS"] = "https://a.example.com, https://b.example.com"
        assert _cors_allow_origins() == ["https://a.example.com", "https://b.example.com"]
    finally:
        os.environ["CORS_ALLOW_ORIGINS"] = "https://allowed.example.com"
        os.environ["PUBLIC_BASE_URL"] = ""
    print("[ratelimit] CORS origin resolution ok")


def test_ratelimit_switch_off():
    ratelimit.reset()

    def probe():
        # 内网链接 400：既证明 SSRF 生效，又避免触网
        return client.get("/api/info", params={"url": "http://127.0.0.1/x"})

    os.environ["RATELIMIT_ENABLED"] = "false"
    try:
        r = probe()
        assert r.status_code == 400
        assert "x-ratelimit-limit" not in r.headers    # 中间件整体跳过
    finally:
        os.environ["RATELIMIT_ENABLED"] = "true"
    r2 = probe()
    assert r2.status_code == 400
    assert r2.headers.get("x-ratelimit-limit") == "60"  # 开关恢复后计数生效
    ratelimit.reset()
    print("[ratelimit] switch off/on ok")


def test_health_field():
    body = client.get("/api/health").json()
    assert body["ratelimit"] is True
    print("[ratelimit] health field ok")


def _cleanup():
    try:
        from backend.storage import db as sdb
        sdb.engine.dispose()
    except Exception:
        pass
    try:
        import shutil
        shutil.rmtree(_TMP_DIR, ignore_errors=True)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        test_policy_lookup()
        test_sliding_window()
        test_heavy_gate()
        test_ip_rate_limit_429()
        test_user_rate_429()
        test_ssrf_rejected_before_cache()
        test_500_desensitized()
        test_cors_whitelist()
        test_cors_origin_resolution()
        test_ratelimit_switch_off()
        test_health_field()
        print("RATELIMIT TEST PASSED")
    finally:
        _cleanup()
