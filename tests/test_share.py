"""分享功能（短链 / 快照 / 落地页 / 封面代理 / QR / 限流）的离线测试（不触网）。

覆盖：
- service：快照消毒（按载体白名单截断、未知键丢弃）、幂等创建（同载体复用旧码 /
           force_new 另发 / notes 按 ref_id 区分）、bundle 聚合载体（多勾选只一条
           链接：同码复用更新内容）、身份字段以 video_infos 缓存为准
           （客户端伪造封面被丢弃）、公开白名单（user_id/id 永不外泄）、CTA 深链
           tab 参数、撤销后 Gone、浏览计数 IP 窗口去重、滑动窗口限流
- router（TestClient）：未登录 401、创建 201/复用 200、列表、撤销后落地页 410 与
           公开 API 410、落地页 XSS 全量转义、落地页浏览计数（IP 去重）、
           封面代理（monkeypatch requests，content-type / 无封面校验）、
           QR 只编码本站链接（域外 400）

运行：python tests/test_share.py
"""
import os
import sys
import tempfile
from pathlib import Path

# 将项目根目录加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 关键：db.py 在「导入时」即按 DATABASE_URL 建 engine，必须在导入任何 backend.* 之前设置。
_TMP_DIR = Path(tempfile.mkdtemp(prefix="mp_share_test_"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP_DIR / 'test_share.db').as_posix()}"
os.environ["AUTH_ENABLED"] = "true"
os.environ["AUTH_ALLOW_ANONYMOUS"] = "false"
os.environ["LOGIN_MAX_ATTEMPTS"] = "100"
os.environ["LOGIN_IP_MAX_PER_WINDOW"] = "100000"   # 功能测试默认不受登录 IP 限流干扰
os.environ["PASSWORD_MIN_LENGTH"] = "8"
os.environ["SHARE_RATE_MAX"] = "100000"            # 功能测试默认不受分享限流干扰
os.environ["PUBLIC_BASE_URL"] = "http://testserver"   # 域名事实源钉到 TestClient base_url，避免本地 .env 部署域名干扰 QR 域锁

from backend import storage  # noqa: E402
from backend.auth import service as auth_service  # noqa: E402
from backend.share import config, service  # noqa: E402
from backend.share.errors import (  # noqa: E402
    GoneError,
    NotFoundError,
    ThrottledError,
    ValidationError,
)
from backend.storage import repo, transcript_key  # noqa: E402


def _raises(exc_type, fn, *args, **kwargs):
    """断言调用抛出指定语义化异常（沿用本仓库「无 pytest 依赖」的写法）。"""
    try:
        fn(*args, **kwargs)
    except exc_type:
        return
    raise AssertionError(f"应抛 {exc_type.__name__}")


def _mk_user(email: str) -> str:
    """注册一个用户，返回其 user_id。"""
    return auth_service.register(email, "password123")["id"]


def test_init_db():
    assert storage.init_db() is True
    print("[db] init_db ok")


# --------------------------------------------------------------------------- #
# 快照消毒
# --------------------------------------------------------------------------- #
def test_snapshot_sanitization():
    """按载体白名单截断：超限裁剪、超量丢弃、未知键不进 payload。"""
    snap = {
        "excerpt": "字" * 600,
        "bullets": ["要点" * 200, "B", "C", "D"],       # 4 条 → 3 条；单条 200 截断
        "top_nodes": [f"n{i}" for i in range(12)],       # 仅 mindmap 载体收录
        "model_label": "M" * 100,
        "hacker_key": "drop me",
    }
    excerpt, payload = service._clean_snapshot("summary", snap, "https://x/v")
    assert len(excerpt) == config.EXCERPT_MAX
    assert len(payload["bullets"]) == config.BULLETS_MAX
    assert len(payload["bullets"][0]) == config.BULLET_MAX
    assert len(payload["model_label"]) == config.MODEL_LABEL_MAX
    assert "top_nodes" not in payload                    # 非 mindmap 载体不收录
    assert "hacker_key" not in payload
    assert payload["normalized_url"] == "https://x/v"

    # mindmap：top_nodes 收录且截断
    _, p2 = service._clean_snapshot("mindmap", snap, "")
    assert len(p2["top_nodes"]) == config.NODES_MAX

    # comments：先截 CMS_MAX 条再逐条校验（非 dict / 空 text 丢弃，likes 负数归零）
    _, p3 = service._clean_snapshot("comments", {
        "comments": [
            {"author": "甲" * 60, "text": "好" * 300, "likes": -5},
            {"author": "乙", "text": ""},                # 空 text → 丢
            {"text": "正常", "likes": 9},                # 无 author → 匿名
            {"author": "丁", "text": "被条数上限截掉"},  # 第 4 条：CMS_MAX=3 先截断
        ],
    }, "")
    assert len(p3["comments"]) == 2
    assert len(p3["comments"][0]["author"]) == config.CM_AUTHOR_MAX
    assert len(p3["comments"][0]["text"]) == config.CM_TEXT_MAX
    assert p3["comments"][0]["likes"] == 0
    assert p3["comments"][1]["author"] == "匿名"

    # qa：q/a 缺一即丢
    _, p4 = service._clean_snapshot("qa", {
        "qa": [{"q": "问" * 300, "a": "答"}, {"q": "只有问"}, {"q": "", "a": "只有答"}],
    }, "")
    assert len(p4["qa"]) == 1
    assert len(p4["qa"][0]["q"]) == config.QA_Q_MAX

    # transcript / notes：数字字段容错（非法 → 0，超界 → 夹紧）
    _, p5 = service._clean_snapshot("transcript", {"segment_count": "abc"}, "")
    assert p5["segment_count"] == 0
    _, p6 = service._clean_snapshot("notes", {"mark_count": 10 ** 9}, "")
    assert p6["mark_count"] == 10 ** 4
    print("[service] snapshot sanitization ok")


# --------------------------------------------------------------------------- #
# 创建 / 幂等 / 身份字段
# --------------------------------------------------------------------------- #
def test_create_and_idempotency():
    """幂等：同 (用户, 视频, 载体, ref) 复用旧码；force_new 另发；notes 按 ref_id 区分。"""
    uid = _mk_user("create@example.com")
    key = transcript_key("https://www.bilibili.com/video/BV1share01")

    s1, created1 = service.create_share(uid, key, "video", url="https://www.bilibili.com/video/BV1share01")
    assert created1 is True and len(s1["code"]) == config.share_code_len()

    s2, created2 = service.create_share(uid, key, "video", url="https://www.bilibili.com/video/BV1share01")
    assert created2 is False and s2["code"] == s1["code"]          # 复用旧码

    s3, created3 = service.create_share(uid, key, "video", force_new=True)
    assert created3 is True and s3["code"] != s1["code"]           # 强制新发

    s4, _ = service.create_share(uid, key, "summary")
    assert s4["code"] != s1["code"]                                # 不同载体互不影响

    # notes：ref_id 是幂等键成员（不同笔记各发各码；video 载体 ref_id 被清空）
    n1, c1 = service.create_share(uid, key, "notes", ref_id="note-a")
    n2, c2 = service.create_share(uid, key, "notes", ref_id="note-b")
    assert c1 and c2 and n1["code"] != n2["code"]
    n1b, c1b = service.create_share(uid, key, "notes", ref_id="note-a")
    assert c1b is False and n1b["code"] == n1["code"]
    v_ref, _ = service.create_share(uid, key, "video", ref_id="ignored")
    assert service.create_share(uid, key, "video")[0]["code"] in (s1["code"], s3["code"])
    assert v_ref["code"] in (s1["code"], s3["code"])               # ref_id 清空后并入 video 幂等键

    # 非法载体 / 缺 content_key → ValidationError
    _raises(ValidationError, service.create_share, uid, key, "hacked")
    _raises(ValidationError, service.create_share, uid, "  ", "video")
    print("[service] create / idempotency / validation ok")


def test_identity_from_info_cache():
    """标题/封面以 video_infos 缓存为准；上报伪造封面被丢弃，非法 thumb 置空。"""
    uid = _mk_user("identity@example.com")
    key = transcript_key("https://www.bilibili.com/video/BV1share02")
    repo.put_info(key, {
        "title": "服务端事实标题",
        "thumbnail": "https://img.example.com/cover.jpg",
    }, url="https://www.bilibili.com/video/BV1share02")

    s, _ = service.create_share(uid, key, "video", snapshot={
        "title": "客户端伪造标题",
        "thumbnail": "https://evil.example.com/x.jpg",   # 非白名单来源键，本就不读
    })
    assert s["title"] == "服务端事实标题"
    assert s["thumb_url"] == "https://img.example.com/cover.jpg"

    # 无缓存时标题回退上报值；thumb 非 http(s) → 置空
    key2 = "k_no_info"
    s2, _ = service.create_share(uid, key2, "video", snapshot={"title": "兜底标题"})
    assert s2["title"] == "兜底标题" and s2["thumb_url"] == ""
    print("[service] identity fields from info cache ok")


def test_service_bundle():
    """bundle 聚合载体：多 section 各自消毒 + 顶层兜底；同码复用更新内容（链接不变）。"""
    uid = _mk_user("bundle@example.com")
    key = transcript_key("https://www.bilibili.com/video/BV1share06")
    repo.put_info(key, {"title": "合辑视频", "thumbnail": "https://img.example.com/b.jpg"},
                  url="https://www.bilibili.com/video/BV1share06")
    snap = {
        "sections": {
            "summary": {"excerpt": "一句话摘要" * 10, "bullets": ["要点1", "要点2", "要点3", "要点4"]},
            "comments": {"comments": [{"author": "甲", "text": "热评", "likes": 7}]},
            "hacked": {"excerpt": "未知载体直接丢弃"},
        },
    }
    s, created = service.create_share(uid, key, "bundle", url="https://www.bilibili.com/video/BV1share06", snapshot=snap)
    assert created is True
    p = s["payload"]
    assert p["kinds"] == ["summary", "comments"]              # 按 KINDS 顺序；未知键丢弃
    assert len(p["sections"]["summary"]["bullets"]) == config.BULLETS_MAX
    assert p["sections"]["comments"]["comments"][0]["likes"] == 7
    assert "hacked" not in p["sections"]
    assert s["excerpt"] == "一句话摘要" * 10                    # 顶层 excerpt 取 summary
    assert p["bullets"] == p["sections"]["summary"]["bullets"]  # 顶层 bullets 兜底（海报/文案兼容）
    assert "normalized_url" not in p["sections"]["summary"]     # 子 payload 不重复存 url

    # 同码复用更新：换勾选（去 comments 加 transcript）→ code 不变、内容跟随
    s2, created2 = service.create_share(uid, key, "bundle", url="https://www.bilibili.com/video/BV1share06",
                                        snapshot={"sections": {"summary": {"excerpt": "新摘要"}, "transcript": {"segment_count": 12}}})
    assert created2 is False and s2["code"] == s["code"]
    assert s2["payload"]["kinds"] == ["summary", "transcript"]
    assert s2["excerpt"] == "新摘要"
    assert "comments" not in s2["payload"]["sections"]

    # bundle 与单载体互不干扰（不同 kind → 不同码）
    v, _ = service.create_share(uid, key, "video", url="https://www.bilibili.com/video/BV1share06")
    assert v["code"] != s["code"]

    # 空 sections / 缺 sections → ValidationError
    _raises(ValidationError, service.create_share, uid, key, "bundle", snapshot={"sections": {}})
    _raises(ValidationError, service.create_share, uid, key, "bundle")
    print("[service] bundle create / same-code update ok")


# --------------------------------------------------------------------------- #
# 公开白名单 / CTA / 撤销 / 浏览去重 / 限流
# --------------------------------------------------------------------------- #
def test_public_dict_whitelist():
    """公开响应字段白名单：无 user_id/id/content_key；thumb 走同源代理；CTA 带 tab 深链。"""
    uid = _mk_user("public@example.com")
    key = transcript_key("https://www.bilibili.com/video/BV1share03")
    repo.put_info(key, {"title": "白名单视频", "thumbnail": "https://img.example.com/c.jpg"},
                  url="https://www.bilibili.com/video/BV1share03")
    s, _ = service.create_share(uid, key, "summary", url="https://www.bilibili.com/video/BV1share03",
                                snapshot={"excerpt": "一句话总结"})

    pub = service.get_public(s["code"], "https://mp.example.com")
    for leaked in ("user_id", "id", "content_key", "revoked_at"):
        assert leaked not in pub, f"公开响应泄漏字段：{leaked}"
    assert pub["thumb_url"] == f"/api/share/{s['code']}/thumb"     # 同源代理，不外泄原始域名
    assert pub["share_url"] == f"https://mp.example.com/s/{s['code']}"
    assert pub["cta_url"].startswith("https://mp.example.com/#/v?url=")
    assert "tab=summary" in pub["cta_url"]                          # 非 video 载体带 tab 深链

    v, _ = service.create_share(uid, key, "video")
    assert "tab=" not in service.get_public(v["code"], "https://mp.example.com")["cta_url"]

    # 撤销 → Gone；不存在 → NotFound
    service.revoke_share(uid, s["code"])
    _raises(GoneError, service.get_public, s["code"], "https://mp.example.com")
    _raises(GoneError, service.thumb_target, s["code"])
    _raises(NotFoundError, service.get_public, "noSuchCode", "https://mp.example.com")
    # 跨用户撤销 → NotFound（不确认存在）
    other = _mk_user("public2@example.com")
    _raises(NotFoundError, service.revoke_share, other, v["code"])
    print("[service] public whitelist / cta / revoke ok")


def test_view_dedup_and_rate():
    """浏览计数 IP 窗口去重；滑动窗口限流超限抛 Throttled（带 retry_after）。"""
    service.reset_throttle_state()
    uid = _mk_user("views@example.com")
    s, _ = service.create_share(uid, "k_views", "video")

    service.register_view(s["code"], "1.1.1.1")
    service.register_view(s["code"], "1.1.1.1")     # 窗口内同 IP → 不重复计数
    service.register_view(s["code"], "2.2.2.2")
    assert service.get_public(s["code"], "http://b")["view_count"] == 2

    # 限流：窗口 2 次，第 3 次抛 Throttled
    os.environ["SHARE_RATE_MAX"] = "2"
    service.reset_throttle_state()
    try:
        service.check_rate("9.9.9.9")
        service.check_rate("9.9.9.9")
        _raises(ThrottledError, service.check_rate, "9.9.9.9")
        service.check_rate("8.8.8.8")               # 其他 IP 不受影响
    finally:
        os.environ["SHARE_RATE_MAX"] = "100000"
        service.reset_throttle_state()
    print("[service] view dedup / rate limit ok")


# --------------------------------------------------------------------------- #
# HTTP 全链路（TestClient）
# --------------------------------------------------------------------------- #
def test_http_unauthenticated_401():
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as client:
        assert client.post("/api/me/shares", json={"content_key": "k"}).status_code == 401
        assert client.get("/api/me/shares").status_code == 401
        assert client.delete("/api/me/shares/anycode").status_code == 401
    print("[http] unauthenticated 401 ok")


def test_http_share_flow():
    """登录态全链路：创建 201 / 复用 200 / 列表 / 落地页 / 公开 API / 撤销后 410。"""
    from fastapi.testclient import TestClient

    from backend.main import app

    service.reset_throttle_state()
    url = "https://www.bilibili.com/video/BV1share04"
    key = transcript_key(url)
    repo.put_info(key, {"title": "HTTP 流程视频", "thumbnail": "https://img.example.com/h.jpg"}, url=url)

    with TestClient(app) as client:
        client.post("/api/auth/register",
                    json={"email": "flow@example.com", "password": "password123", "nickname": None})
        r = client.post("/api/auth/login", json={"identifier": "flow@example.com", "password": "password123"})
        assert r.status_code == 200, r.text

        # 创建（summary 载体，快照含 XSS 探针）→ 201
        r = client.post("/api/me/shares", json={
            "content_key": key, "kind": "summary", "url": url,
            "snapshot": {
                "excerpt": '<script>alert("xss")</script>摘要',
                "bullets": ['<img src=x onerror=alert(1)>要点'],
            },
        })
        assert r.status_code == 201, r.text
        share = r.json()["share"]
        code = share["code"]
        assert "user_id" not in share and share["kind"] == "summary"

        # 同载体再创建 → 200 复用
        r2 = client.post("/api/me/shares", json={"content_key": key, "kind": "summary", "url": url})
        assert r2.status_code == 200 and r2.json()["share"]["code"] == code

        # 列表（管理视角，含完整行）
        lst = client.get("/api/me/shares").json()
        assert lst["total"] >= 1 and any(i["code"] == code for i in lst["items"])

        # 落地页：匿名可读、og meta 齐备、XSS 探针被转义
        service.reset_throttle_state()
        land = client.get(f"/s/{code}")
        assert land.status_code == 200 and "text/html" in land.headers["content-type"]
        html = land.text
        assert "<script>alert(" not in html and "<img src=x onerror" not in html
        assert "&lt;script&gt;" in html                                # 转义后可见
        assert 'property="og:title"' in html and "HTTP 流程视频" in html

        # 公开 API：白名单 + 浏览计数（落地页已计 1 次；同 IP 去重窗口内不再重复 → 恒为 1）
        pub = client.get(f"/api/share/{code}").json()
        assert "user_id" not in pub and pub["view_count"] == 1
        pub2 = client.get(f"/api/share/{code}").json()
        assert pub2["view_count"] == 1                     # 窗口内同 IP 不重复计（含落地页）

        # 撤销 → 落地页 410 / 公开 API 410 / 重复撤销 404
        assert client.delete(f"/api/me/shares/{code}").status_code == 200
        assert client.delete(f"/api/me/shares/{code}").status_code == 404
        assert client.get(f"/s/{code}").status_code == 410
        assert client.get(f"/api/share/{code}").status_code == 410
        # 不存在的码 → 404
        assert client.get("/s/noSuchCode1").status_code == 404
        assert client.get("/api/share/noSuchCode1").status_code == 404
    print("[http] share full flow ok")


def test_http_bundle_flow():
    """bundle HTTP：多勾选只建一条链接；落地页按 Tab 分段渲染；CTA 深链首个非 video Tab。"""
    from fastapi.testclient import TestClient

    from backend.main import app

    service.reset_throttle_state()
    url = "https://www.bilibili.com/video/BV1share07"
    key = transcript_key(url)
    repo.put_info(key, {"title": "合辑分享视频", "thumbnail": "https://img.example.com/b7.jpg"}, url=url)
    with TestClient(app) as client:
        client.post("/api/auth/register", json={"email": "b7@example.com", "password": "password123", "nickname": None})
        client.post("/api/auth/login", json={"identifier": "b7@example.com", "password": "password123"})
        r = client.post("/api/me/shares", json={
            "content_key": key, "kind": "bundle", "url": url,
            "snapshot": {"sections": {
                "video": {"excerpt": "原视频一句话"},
                "summary": {"excerpt": "摘要节选", "bullets": ["要点A"]},
                "comments": {"comments": [{"author": "甲", "text": "评B", "likes": 3}]},
            }},
        })
        assert r.status_code == 201, r.text
        share = r.json()["share"]
        code = share["code"]
        assert share["kind"] == "bundle"
        assert "tab=summary" in share["cta_url"]              # 深链直达首个非 video Tab

        # 换勾选再创建 → 200 同码（单链接契约）；纯 video 合辑不带 tab
        r2 = client.post("/api/me/shares", json={
            "content_key": key, "kind": "bundle", "url": url,
            "snapshot": {"sections": {"video": {"excerpt": "原视频一句话"}}},
        })
        assert r2.status_code == 200 and r2.json()["share"]["code"] == code
        assert "tab=" not in r2.json()["share"]["cta_url"]

        # 恢复多段内容后落地页分段渲染（同码更新跟随最新勾选）
        client.post("/api/me/shares", json={
            "content_key": key, "kind": "bundle", "url": url,
            "snapshot": {"sections": {
                "summary": {"excerpt": "摘要节选", "bullets": ["要点A"]},
                "comments": {"comments": [{"author": "甲", "text": "评B", "likes": 3}]},
            }},
        })
        service.reset_throttle_state()
        land = client.get(f"/s/{code}")
        assert land.status_code == 200
        html = land.text
        assert "sec-h" in html and "AI 总结" in html and "高赞评论" in html   # 分段小标题
        assert "要点A" in html and "评B" in html and "合辑分享视频" in html
    print("[http] bundle single-link flow ok")


def test_http_cross_user_isolation():
    """B 不能撤销 A 的分享（404 不确认存在）；公开端点匿名可读。"""
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as ca, TestClient(app) as cb:
        ca.post("/api/auth/register", json={"email": "sa@example.com", "password": "password123", "nickname": None})
        ca.post("/api/auth/login", json={"identifier": "sa@example.com", "password": "password123"})
        code = ca.post("/api/me/shares", json={"content_key": "k_iso_share", "kind": "video"}).json()["share"]["code"]

        cb.post("/api/auth/register", json={"email": "sb@example.com", "password": "password123", "nickname": None})
        cb.post("/api/auth/login", json={"identifier": "sb@example.com", "password": "password123"})
        assert cb.delete(f"/api/me/shares/{code}").status_code == 404
        assert cb.get("/api/me/shares").json()["total"] == 0

        # A 的分享仍活着，匿名（无登录态的第三个 client）可读落地页
        assert ca.delete(f"/api/me/shares/{code}").status_code == 200
    with TestClient(app) as anon:
        assert anon.get(f"/api/share/{code}").status_code == 410      # 撤销后公开侧 410
    print("[http] cross-user isolation ok")


# --------------------------------------------------------------------------- #
# 封面代理 / QR（monkeypatch requests，不触网）
# --------------------------------------------------------------------------- #
class _FakeUpstream:
    """伪 requests 响应（支持 with 语法 + iter_content 分块）。"""

    def __init__(self, ok=True, mime="image/png", chunks=(b"\x89PNG-fake",)):
        self.ok = ok
        self.headers = {"content-type": mime}
        self._chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, size):
        yield from self._chunks


def test_http_thumb_proxy():
    """封面代理：只读 shares 快照；content-type 校验；无封面 404。"""
    from fastapi.testclient import TestClient

    from backend.main import app

    # backend.share.__init__ 的 router 属性（APIRouter）遮蔽了同名子模块，
    # import as 拿到的是路由对象 → 走 sys.modules 取真实模块（与 test_notes 同坑）。
    router_mod = sys.modules["backend.share.router"]

    service.reset_throttle_state()
    uid = _mk_user("thumb@example.com")
    key = transcript_key("https://www.bilibili.com/video/BV1share05")
    repo.put_info(key, {"title": "封面视频", "thumbnail": "https://img.example.com/t.png"},
                  url="https://www.bilibili.com/video/BV1share05")
    s, _ = service.create_share(uid, key, "video", url="https://www.bilibili.com/video/BV1share05")
    s_no_thumb, _ = service.create_share(uid, "k_no_thumb", "video")

    orig_get = router_mod.requests.get
    try:
        with TestClient(app) as client:
            # 正常回传：字节一致 + 缓存头
            router_mod.requests.get = lambda *a, **k: _FakeUpstream()
            r = client.get(f"/api/share/{s['code']}/thumb")
            assert r.status_code == 200 and r.content == b"\x89PNG-fake"
            assert r.headers["content-type"] == "image/png"
            assert "public" in r.headers.get("cache-control", "")

            # 非图片 content-type → 404（防缓存投毒回传 HTML）
            router_mod.requests.get = lambda *a, **k: _FakeUpstream(mime="text/html")
            assert client.get(f"/api/share/{s['code']}/thumb").status_code == 404

            # 上游失败 → 404
            router_mod.requests.get = lambda *a, **k: _FakeUpstream(ok=False)
            assert client.get(f"/api/share/{s['code']}/thumb").status_code == 404

            # 无封面分享 → 404（不触发上游请求）
            called = []
            router_mod.requests.get = lambda *a, **k: called.append(1)
            assert client.get(f"/api/share/{s_no_thumb['code']}/thumb").status_code == 404
            assert not called
    finally:
        router_mod.requests.get = orig_get
    print("[http] thumb proxy ok")


def test_http_qr_domain_lock():
    """QR 只编码本站分享链接：域外 / 超长 → 400；合法 → SVG。"""
    from fastapi.testclient import TestClient

    from backend.main import app

    service.reset_throttle_state()
    with TestClient(app) as client:
        assert client.get("/api/share/qr", params={"data": "https://evil.example.com/phish"}).status_code == 400
        assert client.get("/api/share/qr", params={"data": "http://x" * 300}).status_code == 400

        base = str(client.base_url).rstrip("/")
        r = client.get("/api/share/qr", params={"data": f"{base}/s/abc123", "box": 4})
        assert r.status_code == 200, r.text
        assert "svg" in r.headers["content-type"] and r.text.lstrip().startswith("<")
    print("[http] qr domain lock ok")


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
        # service 层
        test_snapshot_sanitization()
        test_create_and_idempotency()
        test_identity_from_info_cache()
        test_service_bundle()
        test_public_dict_whitelist()
        test_view_dedup_and_rate()
        # HTTP 层
        test_http_unauthenticated_401()
        test_http_share_flow()
        test_http_bundle_flow()
        test_http_cross_user_isolation()
        test_http_thumb_proxy()
        test_http_qr_domain_lock()
        print("SHARE TEST PASSED")
    finally:
        _cleanup()
