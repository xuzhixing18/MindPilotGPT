"""双通道认证（邮箱 / 手机号）+ 个人资料 + 头像的离线测试（不触网、不需真实 Key）。

覆盖：
- identifiers：邮箱与手机号的各种写法归一为 E.164、单输入框类型识别、脱敏
- service    ：纯手机号注册登录、双标识符注册、重复手机号被拒、资料 CRUD 的
               「传空串清除 vs 不传保持」语义、website 协议白名单、
               换绑/改密需当前密码（失败计入锁定）、改密踢其他设备会话
- avatar     ：魔数校验（拒 SVG / 伪图 / 超限）、一人一文件、读删幂等
- router     ：TestClient 走通 PATCH /me、头像上传→回读→删除、换绑手机号与邮箱

运行：python tests/test_profile.py
"""
import os
import sys
import tempfile
from pathlib import Path

# 将项目根目录加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 关键：db.py 在「导入时」即按 DATABASE_URL 建 engine，必须在导入任何 backend.* 之前设置。
_TMP_DIR = Path(tempfile.mkdtemp(prefix="mp_profile_test_"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP_DIR / 'test_profile.db').as_posix()}"
# 头像必须落到临时目录，否则会写进真实的 data/avatars/
os.environ["AVATAR_DIR"] = (_TMP_DIR / "avatars").as_posix()
os.environ["AVATAR_MAX_BYTES"] = str(64 * 1024)   # 收紧上限，便于用极小数据测出「超限被拒」
os.environ["AUTH_ENABLED"] = "true"
os.environ["AUTH_ALLOW_ANONYMOUS"] = "false"
os.environ["LOGIN_MAX_ATTEMPTS"] = "3"
os.environ["LOGIN_LOCKOUT_MINUTES"] = "15"
os.environ["LOGIN_IP_MAX_PER_WINDOW"] = "1000"    # 功能测试默认不受 IP 限流干扰
os.environ["PASSWORD_MIN_LENGTH"] = "8"
os.environ["PHONE_DEFAULT_REGION"] = "CN"

from backend import storage  # noqa: E402
from backend.auth import avatar, identifiers, service, store, throttle  # noqa: E402
from backend.auth.errors import (  # noqa: E402
    AccountLockedError,
    InvalidCredentialsError,
    UserExistsError,
    ValidationError,
)

# 只校验魔数、不做真实解码，故用「合法文件头 + 填充」即可（不引入 Pillow）
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x11" * 32
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x22" * 32
GIF_BYTES = b"GIF89a" + b"\x33" * 32
WEBP_BYTES = b"RIFF" + (36).to_bytes(4, "little") + b"WEBPVP8 " + b"\x44" * 24
# SVG 是 XML、可内嵌脚本：当图片渲染即存储型 XSS，必须拒（哪怕 Content-Type 谎报 image/png）
SVG_BYTES = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


def _raises(exc_type, fn, *args, **kwargs):
    """断言调用抛出指定语义化异常（沿用本仓库「无 pytest 依赖」的写法）。"""
    try:
        fn(*args, **kwargs)
    except exc_type:
        return
    raise AssertionError(f"应抛 {exc_type.__name__}")


def test_init_db():
    assert storage.init_db() is True
    print("[db] init_db ok")


def test_identifier_normalization():
    # 邮箱：转小写 + 去空白
    assert identifiers.validate_email("  Alice@Example.COM ") == "alice@example.com"
    _raises(ValidationError, identifiers.validate_email, "not-an-email")
    _raises(ValidationError, identifiers.validate_email, "a@b")          # 无顶级域
    _raises(ValidationError, identifiers.validate_email, "")

    # 手机号：各种写法归一为 E.164
    for raw in ("13800138000", "+8613800138000", "+86 138 0013 8000",
                "0086-138-0013-8000", "86 13800138000", "(+86)138.0013.8000"):
        assert identifiers.validate_phone(raw) == "+8613800138000", raw
    _raises(ValidationError, identifiers.validate_phone, "12345")        # 太短
    _raises(ValidationError, identifiers.validate_phone, "1380013800")   # 10 位
    _raises(ValidationError, identifiers.validate_phone, "23800138000")  # 非 1 开头
    _raises(ValidationError, identifiers.validate_phone, "12800138000")  # 第二位非法
    _raises(ValidationError, identifiers.validate_phone, "")
    # 显式带 + 的境外号码不套中国大陆规则
    assert identifiers.validate_phone("+14155552671") == "+14155552671"

    # 单输入框识别：含 @ 即邮箱
    assert identifiers.parse_identifier("a@b.com") == (identifiers.EMAIL, "a@b.com")
    assert identifiers.parse_identifier("13800138000") == (identifiers.PHONE, "+8613800138000")
    _raises(ValidationError, identifiers.parse_identifier, "   ")

    assert identifiers.mask_phone("+8613800138000") == "****8000"
    assert identifiers.mask_phone("138") == "****"           # 不足 4 位时不泄露任何数字
    print("[identifiers] normalize / classify / mask ok")


def test_phone_only_register_and_login():
    """纯手机号账号（email 为 NULL）：可注册、可用手机号登录。"""
    user = service.register(phone="13900139000", password="password123")
    assert user["email"] is None
    assert user["phone"] == "+8613900139000"
    assert user["phone_verified"] is False        # 未接短信服务商，如实标未验证
    assert user["nickname"] == "用户9000"          # 缺省昵称取手机尾号
    assert "password_hash" not in user

    got, token, _ = service.login("13900139000", "password123")
    assert got["id"] == user["id"]
    assert service.resolve_session(token)["phone"] == "+8613900139000"
    _raises(InvalidCredentialsError, service.login, "13900139000", "wrong-pass")
    print("[service] phone-only register / login ok")


def test_both_identifiers_register():
    """邮箱与手机号同时注册：两个标识符都能登录，且都已规范化。"""
    user = service.register("dual@example.com", "password123", "Dual", phone="+86 137 0013 7000")
    assert user["email"] == "dual@example.com" and user["phone"] == "+8613700137000"
    assert user["nickname"] == "Dual"
    assert service.login("DUAL@Example.com", "password123")[0]["id"] == user["id"]
    assert service.login("13700137000", "password123")[0]["id"] == user["id"]
    # 两个标识符都缺 → 拒绝
    _raises(ValidationError, service.register, password="password123")
    print("[service] dual-identifier register / login-by-either ok")


def test_duplicate_identifiers_rejected():
    service.register("dup@example.com", "password123", phone="13600136000")
    _raises(UserExistsError, service.register, "DUP@example.com", "password123")   # 邮箱重复（大小写归一）
    _raises(UserExistsError, service.register, phone="13600136000", password="password123")
    # 同号的不同写法也算重复（规范化后才查重，故不会漏）
    _raises(UserExistsError, service.register, phone="+86 136-0013-6000", password="password123")
    print("[service] duplicate email / phone rejected ok")


def test_profile_update_semantics():
    """exclude_unset 语义：不传的字段保持原样，传空串才是「清除」。"""
    user = service.register("prof@example.com", "password123", "Prof")
    uid = user["id"]

    updated = service.update_profile(uid, {
        "nickname": "小张", "bio": "爱看纪录片", "gender": "male",
        "birthday": "1998-07", "location": "杭州", "website": "example.com",
    })
    assert updated["nickname"] == "小张"
    assert updated["bio"] == "爱看纪录片"
    assert updated["gender"] == "male"
    assert updated["birthday"] == "1998-07-01"           # 仅到月时日补 1
    assert updated["location"] == "杭州"
    assert updated["website"] == "https://example.com"   # 省略协议时自动补 https://

    # 只改一项：其余字段（含 bio）必须原样保留
    partial = service.update_profile(uid, {"location": "上海"})
    assert partial["location"] == "上海" and partial["bio"] == "爱看纪录片"

    # 传空串 = 清除（可空字段回落 None，非空字段回落 ""）
    cleared = service.update_profile(uid, {"website": "", "bio": "", "birthday": ""})
    assert cleared["website"] is None and cleared["bio"] == "" and cleared["birthday"] is None

    # 校验
    _raises(ValidationError, service.update_profile, uid, {"gender": "x"})
    _raises(ValidationError, service.update_profile, uid, {"birthday": "1998年7月"})
    _raises(ValidationError, service.update_profile, uid, {"birthday": "2999-01-01"})
    _raises(ValidationError, service.update_profile, uid, {"bio": "x" * 201})
    _raises(ValidationError, service.update_profile, uid, {"nickname": "  "})
    # 可执行协议必须挡住（前端会把它渲染成 <a href>）
    for bad in ("javascript:alert(1)", "data:text/html,<script>1</script>", "file:///etc/passwd"):
        _raises(ValidationError, service.update_profile, uid, {"website": bad})
    print("[service] profile update / exclude_unset / website whitelist ok")


def test_change_email_and_phone_need_password():
    user = service.register("bind@example.com", "password123", phone="13500135000")
    uid = user["id"]

    # 错误密码 → InvalidCredentialsError，且资料未变
    _raises(InvalidCredentialsError, service.change_email, uid, "bad-pass", "new@example.com")
    assert store.find_user_by_id(uid)["email"] == "bind@example.com"

    ok = service.change_email(uid, "password123", "NEW@Example.com")
    assert ok["email"] == "new@example.com"
    assert ok["email_verified"] is False                 # 新地址所有权尚未证明

    # 改成别人的邮箱 → 409
    service.register("other@example.com", "password123")
    _raises(UserExistsError, service.change_email, uid, "password123", "other@example.com")
    # 改回当前值 → 幂等，不报错
    assert service.change_email(uid, "password123", "new@example.com")["email"] == "new@example.com"

    # 换手机号
    ok = service.change_phone(uid, "password123", "13400134000")
    assert ok["phone"] == "+8613400134000" and ok["phone_verified"] is False
    # 解绑：该账号还有邮箱，允许
    assert service.change_phone(uid, "password123", "")["phone"] is None
    # 纯手机号账号不允许解绑（否则账号再也登不进来）
    phone_only = service.register(phone="13300133000", password="password123")
    _raises(ValidationError, service.change_phone, phone_only["id"], "password123", "")
    print("[service] change email / phone with password confirmation ok")


def test_password_confirm_failure_counts_toward_lockout():
    """换绑/改密的「当前密码」错误也计入锁定计数，否则这条路径能绕过登录锁定爆破。"""
    user = service.register("guard@example.com", "password123")
    uid = user["id"]
    for _ in range(3):
        try:
            service.change_email(uid, "bad-pass", "x@example.com")
        except (InvalidCredentialsError, AccountLockedError):
            pass
    _raises(AccountLockedError, service.change_email, uid, "password123", "x@example.com")
    _raises(AccountLockedError, service.login, "guard@example.com", "password123")
    print("[service] password-confirm failures trigger lockout ok")


def test_change_password_revokes_other_sessions():
    user = service.register("hank@example.com", "password123")
    uid = user["id"]
    _, token_a, _ = service.login("hank@example.com", "password123")   # 当前设备
    _, token_b, _ = service.login("hank@example.com", "password123")   # 另一台设备

    service.change_password(uid, "password123", "newpassword456", current_token=token_a)
    assert service.resolve_session(token_a) is not None    # 不踢自己
    assert service.resolve_session(token_b) is None        # 其他设备全部失效

    _raises(InvalidCredentialsError, service.login, "hank@example.com", "password123")
    assert service.login("hank@example.com", "newpassword456")[0]["id"] == uid
    # 当前密码错 / 新旧密码相同
    _raises(InvalidCredentialsError, service.change_password, uid, "bad", "another-pass1")
    _raises(ValidationError, service.change_password, uid, "newpassword456", "newpassword456")
    print("[service] change password revokes other sessions ok")


def test_avatar_sniff_and_lifecycle():
    assert avatar.sniff_image(PNG_BYTES) == ("png", "image/png")
    assert avatar.sniff_image(JPEG_BYTES) == ("jpg", "image/jpeg")
    assert avatar.sniff_image(GIF_BYTES) == ("gif", "image/gif")
    assert avatar.sniff_image(WEBP_BYTES) == ("webp", "image/webp")
    assert avatar.sniff_image(SVG_BYTES) is None           # XML 可嵌脚本 → 明确拒
    assert avatar.sniff_image(b"<html><body>hi") is None
    assert avatar.sniff_image(b"") is None

    uid = "f" * 32
    _raises(ValidationError, avatar.save_avatar, uid, b"")            # 空文件
    _raises(ValidationError, avatar.save_avatar, uid, SVG_BYTES)      # 非法格式
    _raises(ValidationError, avatar.save_avatar, uid, PNG_BYTES + b"\x00" * (64 * 1024))
    _raises(ValidationError, avatar.save_avatar, "../etc/passwd", PNG_BYTES)   # 路径穿越

    url, mime = avatar.save_avatar(uid, PNG_BYTES)
    assert mime == "image/png"
    assert url.startswith(f"/api/auth/avatar/{uid}?v=")
    assert avatar.read_avatar(uid) == (PNG_BYTES, "image/png")

    # 换格式上传：旧扩展名残留被清掉 → 恒定「一人一文件」
    url2, mime2 = avatar.save_avatar(uid, JPEG_BYTES)
    assert mime2 == "image/jpeg" and url2 != url           # 版本参数每次都换（缓存破坏）
    stored = [p.name for p in Path(os.environ["AVATAR_DIR"]).iterdir() if p.name.startswith(uid)]
    assert stored == [f"{uid}.jpg"], stored

    assert avatar.delete_avatar(uid) is True
    assert avatar.delete_avatar(uid) is False              # 幂等
    assert avatar.read_avatar(uid) is None
    print("[avatar] magic sniff / one-file-per-user / delete idempotent ok")


def test_set_and_remove_avatar_via_service():
    user = service.register("ava@example.com", "password123")
    uid = user["id"]
    assert user["avatar_url"] is None

    updated = service.set_avatar(uid, PNG_BYTES)
    assert updated["avatar_url"].startswith(f"/api/auth/avatar/{uid}?v=")
    assert store.find_user_by_id(uid)["avatar_url"] == updated["avatar_url"]

    removed = service.remove_avatar(uid)
    assert removed["avatar_url"] is None
    assert avatar.read_avatar(uid) is None
    print("[service] set / remove avatar ok")


def test_http_profile_and_avatar_flow():
    """端到端（TestClient）：手机号注册登录 → 改资料 → 传头像 → 回读 → 删除。"""
    from fastapi.testclient import TestClient

    from backend.main import app

    throttle.login_limiter.reset()
    with TestClient(app) as client:
        phone = "13200132000"
        r = client.post("/api/auth/register",
                        json={"phone": phone, "password": "password123", "nickname": None})
        assert r.status_code == 201, r.text
        assert r.json()["user"]["phone"] == "+8613200132000"

        # 未登录 → 401
        assert client.get("/api/auth/me").status_code == 401
        # 用手机号登录（identifier 单框），旧的 email 字段仍兼容
        r = client.post("/api/auth/login", json={"identifier": phone, "password": "password123"})
        assert r.status_code == 200, r.text
        assert r.json()["user"]["email"] is None

        # PATCH /me：只提交显式字段
        r = client.patch("/api/auth/me", json={
            "nickname": "小张", "bio": "爱看纪录片", "gender": "female",
            "birthday": "1998-07", "location": "杭州", "website": "example.com",
        })
        assert r.status_code == 200, r.text
        u = r.json()["user"]
        assert (u["nickname"], u["gender"], u["birthday"]) == ("小张", "female", "1998-07-01")
        assert u["website"] == "https://example.com"
        # 不传的字段保持原样
        u = client.patch("/api/auth/me", json={"location": "上海"}).json()["user"]
        assert u["location"] == "上海" and u["bio"] == "爱看纪录片"
        # 传空串清除
        assert client.patch("/api/auth/me", json={"website": ""}).json()["user"]["website"] is None
        # 非法值 → 400（语义化异常统一映射）
        assert client.patch("/api/auth/me",
                            json={"website": "javascript:alert(1)"}).status_code == 400
        assert client.patch("/api/auth/me", json={"gender": "x"}).status_code == 400

        # 头像上传：按魔数判定，Content-Type 谎报无效
        r = client.post("/api/auth/me/avatar", files={"file": ("a.png", PNG_BYTES, "image/png")})
        assert r.status_code == 200, r.text
        url = r.json()["user"]["avatar_url"]
        got = client.get(url)
        assert got.status_code == 200 and got.content == PNG_BYTES
        assert got.headers["content-type"] == "image/png"
        assert "immutable" in got.headers["cache-control"]     # URL 带版本参数，可长缓存

        r = client.post("/api/auth/me/avatar", files={"file": ("x.png", SVG_BYTES, "image/png")})
        assert r.status_code == 400, r.text                    # 伪图被拒
        r = client.post("/api/auth/me/avatar",
                        files={"file": ("big.png", PNG_BYTES + b"\x00" * (64 * 1024), "image/png")})
        assert r.status_code == 400, r.text                    # 超限被拒

        # 换绑手机号（需当前密码）
        r = client.post("/api/auth/me/phone", json={"phone": "13100131000", "password": "password123"})
        assert r.status_code == 200, r.text
        assert r.json()["user"]["phone"] == "+8613100131000"
        assert client.post("/api/auth/me/phone",
                           json={"phone": "13000130000", "password": "bad"}).status_code == 401
        # 换绑邮箱
        r = client.post("/api/auth/me/email", json={"email": "now@example.com", "password": "password123"})
        assert r.status_code == 200, r.text
        assert r.json()["user"]["email"] == "now@example.com"

        # 删除头像 → 回读 404
        assert client.delete("/api/auth/me/avatar").status_code == 200
        assert client.get(url).status_code == 404

        # 改密后当前 Cookie 仍有效（不踢自己）
        r = client.post("/api/auth/me/password",
                        json={"current_password": "password123", "new_password": "brandnew789"})
        assert r.status_code == 200, r.text
        assert client.get("/api/auth/me").status_code == 200
        # 登出 → 401
        assert client.post("/api/auth/logout").status_code == 200
        assert client.get("/api/auth/me").status_code == 401
        assert client.patch("/api/auth/me", json={"nickname": "x"}).status_code == 401
    print("[http] profile / avatar / rebind / password flow ok")


def test_unauthenticated_avatar_endpoints():
    """头像读取公开、写入需登录；不存在的头像 404 而非 500。"""
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as client:
        assert client.get("/api/auth/avatar/" + "0" * 32).status_code == 404
        assert client.post("/api/auth/me/avatar",
                           files={"file": ("a.png", PNG_BYTES, "image/png")}).status_code == 401
        assert client.delete("/api/auth/me/avatar").status_code == 401
    print("[http] avatar read public / write requires login ok")


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
        test_identifier_normalization()
        test_phone_only_register_and_login()
        test_both_identifiers_register()
        test_duplicate_identifiers_rejected()
        test_profile_update_semantics()
        test_change_email_and_phone_need_password()
        test_password_confirm_failure_counts_toward_lockout()
        test_change_password_revokes_other_sessions()
        test_avatar_sniff_and_lifecycle()
        test_set_and_remove_avatar_via_service()
        test_http_profile_and_avatar_flow()
        test_unauthenticated_avatar_endpoints()
        print("PROFILE TEST PASSED")
    finally:
        _cleanup()
