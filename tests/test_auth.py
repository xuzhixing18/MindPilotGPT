"""认证地基（auth 包）的离线测试（不触网、不需真实 Key / Redis）。

覆盖：
- security ：密码哈希/校验往返、错误密码、令牌哈希稳定、方案前缀识别
- service  ：注册（含校验/重复）、登录成功、会话解析、登出撤销、失败锁定
- throttle ：单 IP 登录滑动窗口超限抛 ThrottledError
- 依赖      ：get_current_user 无 Cookie 时匿名、require_user 未登录抛错
- 路由      ：TestClient 走通 register → login(下发Cookie) → me → logout → me(401)
- 记住我    ：remember=true 时会话有效期更长（remember_ttl_hours）
- 业务门禁  ：auth_required 时未登录访问 /api/info 等一律 401，登录后放行；开关关闭则放行

运行：python tests/test_auth.py
"""
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# 将项目根目录加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 关键：db.py 在「导入时」即按 DATABASE_URL 建 engine，必须在导入任何 backend.* 之前设置临时库。
_TMP_DIR = Path(tempfile.mkdtemp(prefix="mp_auth_test_"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP_DIR / 'test_auth.db').as_posix()}"
os.environ["AUTH_ENABLED"] = "true"
os.environ["AUTH_ALLOW_ANONYMOUS"] = "false"  # 显式设定，不受开发者 .env 影响
os.environ["SESSION_TTL_HOURS"] = "168"
os.environ["REMEMBER_TTL_HOURS"] = "720"
os.environ["LOGIN_MAX_ATTEMPTS"] = "3"        # 便于快速触发锁定
os.environ["LOGIN_LOCKOUT_MINUTES"] = "15"
os.environ["LOGIN_IP_MAX_PER_WINDOW"] = "1000"  # 功能测试默认不受 IP 限流干扰
os.environ["PASSWORD_MIN_LENGTH"] = "8"

from backend import storage  # noqa: E402
from backend.auth import security, service, store, throttle  # noqa: E402
from backend.auth.config import load_settings  # noqa: E402
from backend.auth.dependencies import get_current_user, require_user  # noqa: E402
from backend.auth.errors import (  # noqa: E402
    AccountLockedError,
    InvalidCredentialsError,
    NotAuthenticatedError,
    ThrottledError,
    UserExistsError,
    ValidationError,
)


def test_init_db():
    assert storage.init_db() is True
    print("[db] init_db ok（users / sessions 建表）")


def test_security_password():
    h = security.hash_password("correct-horse-battery")
    assert h and h != "correct-horse-battery"          # 不可逆
    assert security.verify_password("correct-horse-battery", h) is True
    assert security.verify_password("wrong-password", h) is False
    assert security.verify_password("x", "") is False    # 空哈希不匹配
    # 方案前缀可识别（argon2id 或 PBKDF2 回退）
    assert h.startswith("$argon2") or h.startswith("pbkdf2_sha256$")
    print(f"[security] password hash/verify ok（argon2={security.argon2_available()}）")


def test_security_token():
    t = security.new_session_token()
    assert len(t) >= 32
    assert security.new_session_token() != t             # 高熵不重复
    assert security.hash_token(t) == security.hash_token(t)  # 稳定
    assert security.hash_token(t) != security.hash_token("other")
    assert len(security.new_id()) == 32
    print("[security] token gen/hash ok")


def test_register_validation_and_duplicate():
    service.register("alice@example.com", "password123", "Alice")
    # 重复注册（大小写归一）
    try:
        service.register("ALICE@example.com", "password123")
        assert False, "重复邮箱应抛 UserExistsError"
    except UserExistsError:
        pass
    # 邮箱格式非法
    try:
        service.register("not-an-email", "password123")
        assert False, "非法邮箱应抛 ValidationError"
    except ValidationError:
        pass
    # 密码过短
    try:
        service.register("bob@example.com", "123")
        assert False, "过短密码应抛 ValidationError"
    except ValidationError:
        pass
    print("[service] register validation / duplicate ok")


def test_login_and_session_roundtrip():
    service.register("carol@example.com", "password123", "Carol")
    user, token, expires = service.login("carol@example.com", "password123", ip="1.2.3.4")
    assert user["email"] == "carol@example.com"
    assert "password_hash" not in user                   # 回传不含敏感字段
    assert token and expires is not None

    # 会话可解析回用户
    resolved = service.resolve_session(token)
    assert resolved is not None and resolved["id"] == user["id"]

    # 错误密码
    try:
        service.login("carol@example.com", "wrong-pass")
        assert False, "错误密码应抛 InvalidCredentialsError"
    except InvalidCredentialsError:
        pass

    # 登出后会话失效
    service.logout(token)
    assert service.resolve_session(token) is None
    print("[service] login / session resolve / logout ok")


def test_login_nonexistent_user():
    try:
        service.login("nobody@example.com", "password123")
        assert False, "不存在用户应抛 InvalidCredentialsError（防枚举）"
    except InvalidCredentialsError:
        pass
    print("[service] nonexistent user → InvalidCredentials ok")


def test_account_lockout():
    service.register("dave@example.com", "password123", "Dave")
    settings = load_settings()
    # 连续错误密码达阈值 → 触发锁定
    for _ in range(settings.login_max_attempts):
        try:
            service.login("dave@example.com", "bad-pass")
        except InvalidCredentialsError:
            pass
        except AccountLockedError:
            break
    # 此后即便密码正确也应被锁定拒绝
    try:
        service.login("dave@example.com", "password123")
        assert False, "锁定后应抛 AccountLockedError"
    except AccountLockedError:
        pass
    print("[service] account lockout after max attempts ok")


def test_throttle_sliding_window():
    throttle.login_limiter.reset()
    os.environ["LOGIN_IP_MAX_PER_WINDOW"] = "3"
    os.environ["LOGIN_IP_WINDOW_SECONDS"] = "60"
    try:
        for _ in range(3):
            throttle.throttle_login("9.9.9.9")   # 前 3 次放行
        try:
            throttle.throttle_login("9.9.9.9")   # 第 4 次超限
            assert False, "超限应抛 ThrottledError"
        except ThrottledError as exc:
            assert exc.retry_after >= 1
        # 不同 IP 不受影响
        throttle.throttle_login("8.8.8.8")
    finally:
        os.environ["LOGIN_IP_MAX_PER_WINDOW"] = "1000"
        throttle.login_limiter.reset()
    print("[throttle] login IP sliding-window limit ok")


def test_dependencies_anonymous_and_require():
    from fastapi import Request

    # 无 Cookie 的请求 → 匿名
    scope = {"type": "http", "headers": [], "client": ("127.0.0.1", 123), "query_string": b""}
    req = Request(scope)
    user = get_current_user(req)
    assert user.is_anonymous and not user.is_authenticated
    # require_user 对匿名主体抛 NotAuthenticatedError
    try:
        require_user(user)
        assert False, "匿名应被 require_user 拒绝"
    except NotAuthenticatedError:
        pass
    print("[deps] anonymous get_current_user / require_user ok")


def test_http_flow_with_testclient():
    from fastapi.testclient import TestClient

    from backend.main import app

    throttle.login_limiter.reset()
    with TestClient(app) as client:
        email = "erin@example.com"
        # 注册
        r = client.post("/api/auth/register", json={"email": email, "password": "password123", "nickname": "Erin"})
        assert r.status_code == 201, r.text
        assert r.json()["user"]["email"] == email
        # 未登录访问 /me → 401
        assert client.get("/api/auth/me").status_code == 401
        # 登录 → 下发 Cookie
        r = client.post("/api/auth/login", json={"email": email, "password": "password123"})
        assert r.status_code == 200, r.text
        # 带 Cookie 访问 /me → 200
        r = client.get("/api/auth/me")
        assert r.status_code == 200, r.text
        assert r.json()["user"]["email"] == email
        # 登出 → 再次 /me 401
        assert client.post("/api/auth/logout").status_code == 200
        assert client.get("/api/auth/me").status_code == 401
        # 健康检查暴露 auth 开关
        assert client.get("/api/health").json().get("auth") is True
    print("[http] register/login/me/logout flow via TestClient ok")


def test_remember_me_ttl():
    """记住我：同一账号两次登录，remember=true 的过期时间应明显更晚。"""
    service.register("frank@example.com", "password123", "Frank")
    settings = load_settings()
    assert settings.remember_ttl_hours > settings.session_ttl_hours
    now = datetime.now(timezone.utc)
    _, _, exp_plain = service.login("frank@example.com", "password123")
    _, _, exp_remember = service.login("frank@example.com", "password123", remember=True)
    h_plain = (exp_plain - now).total_seconds() / 3600
    h_remember = (exp_remember - now).total_seconds() / 3600
    # 允许少量执行耗时误差（<6 分钟）
    assert abs(h_plain - settings.session_ttl_hours) < 0.1, h_plain
    assert abs(h_remember - settings.remember_ttl_hours) < 0.1, h_remember
    print(f"[service] remember-me ttl ok（{h_plain:.0f}h → {h_remember:.0f}h）")


def test_business_endpoint_gate():
    """业务端点门禁：未登录 401 / 登录后放行 / 开关关闭放行。

    探针技巧：用「空 url」请求 /api/info——门禁依赖在参数校验之前执行，所以：
    401 = 被门禁拦住；422 = 已通过门禁（因 url 不合法而校验失败）——全程不触网。
    """
    from fastapi.testclient import TestClient

    from backend.main import app

    throttle.login_limiter.reset()
    probe = "/api/info?url="
    try:
        with TestClient(app) as client:
            # health 始终开放，并暴露前后端共用的判据
            h = client.get("/api/health").json()
            assert h.get("auth") is True and h.get("auth_required") is True

            # 1) 强制登录：未登录 → 401
            assert client.get(probe).status_code == 401

            # 2) 登录后 → 通过门禁（422 来自 url 校验，说明已进入端点）
            email = "gate@example.com"
            assert client.post("/api/auth/register",
                               json={"email": email, "password": "password123"}).status_code == 201
            r = client.post("/api/auth/login", json={"email": email, "password": "password123"})
            assert r.status_code == 200, r.text
            assert r.json().get("expires_at")            # 前端可据此展示有效期
            assert client.get(probe).status_code == 422

            # 3) 登出后（会话已撤销）→ 再次 401
            assert client.post("/api/auth/logout").status_code == 200
            assert client.get(probe).status_code == 401

            # 4) 允许匿名 → 未登录也放行（走免费额度）
            os.environ["AUTH_ALLOW_ANONYMOUS"] = "true"
            assert client.get(probe).status_code == 422
            assert client.get("/api/health").json().get("auth_required") is False

            # 5) 鉴权总开关关闭 → 完全放行（与改造前一致）
            os.environ["AUTH_ENABLED"] = "false"
            assert client.get(probe).status_code == 422
            assert client.get("/api/health").json().get("auth") is False
    finally:
        # 复原，避免影响其他用例
        os.environ["AUTH_ENABLED"] = "true"
        os.environ["AUTH_ALLOW_ANONYMOUS"] = "false"
        throttle.login_limiter.reset()
    print("[gate] business endpoints 401 when logged out / pass when logged in ok")


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
        test_init_db()
        test_security_password()
        test_security_token()
        test_register_validation_and_duplicate()
        test_login_and_session_roundtrip()
        test_login_nonexistent_user()
        test_account_lockout()
        test_throttle_sliding_window()
        test_dependencies_anonymous_and_require()
        test_http_flow_with_testclient()
        test_remember_me_ttl()
        test_business_endpoint_gate()
        print("AUTH TEST PASSED")
    finally:
        _cleanup()
