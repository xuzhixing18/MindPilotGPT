"""用户私有资源（处理历史 / 合集 / 问答会话）的离线测试（不触网、不需真实 Key）。

覆盖：
- store/service：历史 kinds 聚合、跨用户列级隔离、超限滚动淘汰、搜索/kind 过滤、
                 删历史级联本人 QA 会话、合集 CRUD 与上限、入集去重、QA 会话生命周期、
                 删光会话后 qa kind 同步移除、侧边栏聚合
- ask_in_session：服务端从 DB 组装上下文（不信任前端 history）、消息落库截断
- router（TestClient）：未登录一律 401、登录态 /api/me/* 全链路、跨用户访问 404

运行：python tests/test_library.py
"""
import os
import sys
import tempfile
from pathlib import Path

# 将项目根目录加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 关键：db.py 在「导入时」即按 DATABASE_URL 建 engine，必须在导入任何 backend.* 之前设置。
_TMP_DIR = Path(tempfile.mkdtemp(prefix="mp_library_test_"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP_DIR / 'test_library.db').as_posix()}"
os.environ["AVATAR_DIR"] = (_TMP_DIR / "avatars").as_posix()
os.environ["AUTH_ENABLED"] = "true"
os.environ["AUTH_ALLOW_ANONYMOUS"] = "false"
os.environ["LOGIN_MAX_ATTEMPTS"] = "100"
os.environ["LOGIN_IP_MAX_PER_WINDOW"] = "100000"   # 功能测试默认不受 IP 限流干扰
os.environ["PASSWORD_MIN_LENGTH"] = "8"
os.environ["PHONE_DEFAULT_REGION"] = "CN"

from backend import storage  # noqa: E402
from backend.auth import service as auth_service  # noqa: E402
from backend.library import config, service, store  # noqa: E402
from backend.library.errors import (  # noqa: E402
    CapExceededError,
    NotFoundError,
    ValidationError,
)
from backend.storage import transcript_key  # noqa: E402


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
# 处理历史
# --------------------------------------------------------------------------- #
def test_record_action_aggregates_kinds():
    """同一视频多次产出：kinds 累加去重、title 快照非空则覆盖、updated_at 刷新。"""
    uid = _mk_user("hist@example.com")
    url = "https://www.bilibili.com/video/BV1hist0001"
    key = transcript_key(url)

    e1 = service.record_action(uid, url, "标题A", "transcribe", "bilibili")
    assert e1["content_key"] == key and e1["kinds"] == ["transcribe"]
    assert e1["title"] == "标题A" and e1["source"] == "bilibili"

    e2 = service.record_action(uid, url, "标题B", "summary", "bilibili")
    assert e2["kinds"] == ["transcribe", "summary"]       # 累加
    assert e2["title"] == "标题B"                          # 非空覆盖

    e3 = service.record_action(uid, url, "", "summary", "")  # 重复 kind + 空 title
    assert e3["kinds"] == ["transcribe", "summary"]        # 去重
    assert e3["title"] == "标题B"                           # 空则不覆盖

    # 未知 kind / 空 user_id 静默跳过（返回 None，不写库）
    assert service.record_action(uid, url, "x", "download") is None
    assert service.record_action("", url, "x", "summary") is None
    print("[history] record_action kinds aggregation ok")


def test_history_isolation_between_users():
    """列级隔离：A 的历史 B 完全看不到，且删不到。"""
    a = _mk_user("a@example.com")
    b = _mk_user("b@example.com")
    url = "https://v.douyin.com/iso0001"
    key = transcript_key(url)
    service.record_action(a, url, "A的视频", "summary", "douyin")

    assert store.history_get(a, key)["title"] == "A的视频"
    assert store.history_get(b, key) is None               # B 取不到
    assert service.list_history(b)["total"] == 0
    _raises(NotFoundError, service.get_history, b, key)    # B 详情 → NotFound
    _raises(NotFoundError, service.delete_history, b, key)  # B 删除 → NotFound（不确认存在）
    print("[history] cross-user isolation ok")


def test_history_rolling_eviction():
    """超限滚动淘汰最旧；淘汰级联其 QA 会话。"""
    os.environ["HISTORY_MAX_ENTRIES"] = "3"
    try:
        uid = _mk_user("roll@example.com")
        keys = []
        for i in range(4):
            url = f"https://www.bilibili.com/video/BV1roll{i:04d}"
            keys.append(transcript_key(url))
            service.record_action(uid, url, f"视频{i}", "transcribe")
        # 最旧的第 0 条被淘汰，其余 3 条留存
        assert service.list_history(uid)["total"] == 3
        assert store.history_get(uid, keys[0]) is None
        for k in keys[1:]:
            assert store.history_get(uid, k) is not None
    finally:
        os.environ.pop("HISTORY_MAX_ENTRIES", None)
    print("[history] rolling eviction ok")


def test_history_search_and_kind_filter():
    uid = _mk_user("search@example.com")
    u1 = "https://www.bilibili.com/video/BV1search01"
    u2 = "https://v.douyin.com/search02"
    service.record_action(uid, u1, "深度学习入门", "summary", "bilibili")
    service.record_action(uid, u2, "美食探店", "transcribe", "douyin")

    # q 命中 title（LIKE 模糊）
    got = service.list_history(uid, q="深度")
    assert got["total"] == 1 and got["items"][0]["title"] == "深度学习入门"
    # q 命中 url
    assert service.list_history(uid, q="douyin")["total"] == 1
    # kind 过滤
    assert service.list_history(uid, kind="summary")["total"] == 1
    assert service.list_history(uid, kind="mindmap")["total"] == 0
    # 分页
    page = service.list_history(uid, limit=1, offset=1)
    assert page["total"] == 2 and len(page["items"]) == 1
    print("[history] search / kind filter / pagination ok")


def test_history_delete_cascades_qa_sessions():
    """删历史条目：级联本人该视频的 QA 会话与消息。"""
    uid = _mk_user("cascade@example.com")
    url = "https://www.bilibili.com/video/BV1casc0001"
    key = transcript_key(url)
    service.record_action(uid, url, "级联视频", "transcribe")
    s1 = service.create_session(uid, key, "级联视频")
    s2 = service.create_session(uid, key, "另一个会话")
    assert len(service.list_sessions(uid, key)) == 2

    service.delete_history(uid, key)
    assert store.history_get(uid, key) is None
    assert service.list_sessions(uid, key) == []           # 会话被级联清除
    _raises(NotFoundError, service.get_session, uid, s1["id"])
    _raises(NotFoundError, service.get_session, uid, s2["id"])
    print("[history] delete cascades qa sessions ok")


def test_clear_history():
    uid = _mk_user("clear@example.com")
    for i in range(3):
        service.record_action(uid, f"https://x.com/v/{i}", f"v{i}", "summary")
    service.create_session(uid, transcript_key("https://x.com/v/0"), "s")
    n = service.clear_history(uid)
    assert n == 3
    assert service.list_history(uid)["total"] == 0
    assert service.list_sessions(uid) == []
    print("[history] clear all ok")


# --------------------------------------------------------------------------- #
# 合集
# --------------------------------------------------------------------------- #
def test_collections_crud_and_validation():
    uid = _mk_user("coll@example.com")
    coll = service.create_collection(uid, "  我的收藏  ", "  描述  ")
    assert coll["name"] == "我的收藏" and coll["description"] == "描述"   # 去空白

    # 名称校验
    _raises(ValidationError, service.create_collection, uid, "   ")       # 空
    _raises(ValidationError, service.create_collection, uid, "x" * 41)    # 超 40
    # 改名 / 改描述（None 表示不改）
    updated = service.update_collection(uid, coll["id"], "新名字", None)
    assert updated["name"] == "新名字" and updated["description"] == "描述"

    got = service.get_collection(uid, coll["id"])
    assert got["name"] == "新名字" and got["count"] == 0
    assert service.list_collections(uid)[0]["id"] == coll["id"]

    service.delete_collection(uid, coll["id"])
    _raises(NotFoundError, service.get_collection, uid, coll["id"])
    _raises(NotFoundError, service.delete_collection, uid, coll["id"])
    print("[collections] crud / validation ok")


def test_collections_cap():
    os.environ["COLLECTIONS_MAX_PER_USER"] = "2"
    try:
        uid = _mk_user("collcap@example.com")
        service.create_collection(uid, "c1")
        service.create_collection(uid, "c2")
        _raises(CapExceededError, service.create_collection, uid, "c3")
    finally:
        os.environ.pop("COLLECTIONS_MAX_PER_USER", None)
    print("[collections] per-user cap ok")


def test_collection_items_add_dup_remove():
    uid = _mk_user("items@example.com")
    other = _mk_user("items-other@example.com")
    coll = service.create_collection(uid, "条目集")
    cid = coll["id"]
    url = "https://www.bilibili.com/video/BV1item0001"
    key = transcript_key(url)

    item = service.add_item(uid, cid, key, url, "条目视频")
    assert item["content_key"] == key and item["title"] == "条目视频"
    # 重复入集 → ValidationError
    _raises(ValidationError, service.add_item, uid, cid, key, url, "条目视频")
    # 空 content_key → ValidationError
    _raises(ValidationError, service.add_item, uid, cid, "", url, "x")
    # 入不存在的合集 → NotFound
    _raises(NotFoundError, service.add_item, uid, "nope", key, url, "x")
    # 别的用户往我的合集塞 → NotFound（合集不属于他）
    _raises(NotFoundError, service.add_item, other, cid, "anotherkey", "u", "t")

    assert service.get_collection(uid, cid, with_items=True)["items"][0]["content_key"] == key
    # 删历史不影响合集条目（快照解耦）
    service.record_action(uid, url, "条目视频", "transcribe")
    service.delete_history(uid, key)
    assert store.item_count(uid, cid) == 1

    service.remove_item(uid, cid, key)
    assert store.item_count(uid, cid) == 0
    _raises(NotFoundError, service.remove_item, uid, cid, key)             # 再删 → NotFound
    print("[collections] item add / dup / remove / decoupled-from-history ok")


def test_collection_items_cap():
    os.environ["COLLECTION_ITEMS_MAX"] = "2"
    try:
        uid = _mk_user("itemcap@example.com")
        cid = service.create_collection(uid, "小集")["id"]
        service.add_item(uid, cid, "k1")
        service.add_item(uid, cid, "k2")
        _raises(CapExceededError, service.add_item, uid, cid, "k3")
    finally:
        os.environ.pop("COLLECTION_ITEMS_MAX", None)
    print("[collections] item cap ok")


def test_collection_isolation():
    a = _mk_user("ca@example.com")
    b = _mk_user("cb@example.com")
    cid = service.create_collection(a, "A的合集")["id"]
    _raises(NotFoundError, service.get_collection, b, cid)
    _raises(NotFoundError, service.update_collection, b, cid, "劫持", None)
    _raises(NotFoundError, service.delete_collection, b, cid)
    assert service.list_collections(b) == []
    print("[collections] cross-user isolation ok")


# --------------------------------------------------------------------------- #
# 问答会话（stub 掉真实 LLM）
# --------------------------------------------------------------------------- #
class _AskStub:
    """替换 ai.qa.ask：记录每次收到的 history，返回固定答案（不触网）。"""

    def __init__(self):
        self.calls = []

    def __call__(self, text, title="", question="", *, history=None):
        self.calls.append({"text": text, "title": title, "question": question, "history": history})
        return {"answer": f"答：{question}", "model": "stub", "question": question}


def test_qa_session_lifecycle_and_context_assembly():
    stub = _AskStub()
    original = service.ai_qa.ask
    service.ai_qa.ask = stub
    try:
        uid = _mk_user("qa@example.com")
        url = "https://www.bilibili.com/video/BV1qa000001"
        key = transcript_key(url)

        # 首轮：无 session_id → 自动新建，history 为 None
        result, sid = service.ask_in_session(uid, None, key, "QA视频", "字幕全文", "第一问")
        assert result["answer"] == "答：第一问" and sid
        assert stub.calls[-1]["history"] is None

        # 续聊：服务端从 DB 组装上一轮上下文（不信任前端 history）
        service.ask_in_session(uid, sid, key, "QA视频", "字幕全文", "第二问")
        hist = stub.calls[-1]["history"]
        assert hist and hist[0] == {"role": "user", "content": "第一问"}
        assert hist[1]["role"] == "assistant" and hist[1]["content"] == "答：第一问"

        # 消息落库、qa kind 同步进历史
        msgs = store.qa_messages(uid, sid)
        assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
        assert "qa" in store.history_get(uid, key)["kinds"]

        # 会话不属于本人 → NotFound
        other = _mk_user("qa-other@example.com")
        _raises(NotFoundError, service.ask_in_session, other, sid, key, "x", "y", "z")

        # 改名
        assert service.rename_session(uid, sid, "  新会话名  ")["title"] == "新会话名"
        _raises(ValidationError, service.rename_session, uid, sid, "   ")

        # 删会话 → 该视频再无会话时移除 qa kind
        service.delete_session(uid, sid)
        _raises(NotFoundError, service.get_session, uid, sid)
        assert "qa" not in store.history_get(uid, key)["kinds"]
    finally:
        service.ai_qa.ask = original
    print("[qa] session lifecycle / db-context / kind-sync / isolation ok")


def test_qa_message_truncation():
    stub = _AskStub()
    original = service.ai_qa.ask
    service.ai_qa.ask = stub
    try:
        uid = _mk_user("qatrunc@example.com")
        key = transcript_key("https://x.com/trunc")
        long_q = "问" * (config.MESSAGE_MAX_CHARS + 500)
        _sid = service.ask_in_session(uid, None, key, "t", "字幕", long_q)[1]
        saved = store.qa_messages(uid, _sid)
        assert len(saved[0]["content"]) == config.MESSAGE_MAX_CHARS      # 落库被截断
    finally:
        service.ai_qa.ask = original
    print("[qa] message truncation ok")


def test_qa_session_rolling():
    os.environ["QA_SESSIONS_MAX_PER_USER"] = "2"
    try:
        uid = _mk_user("qaroll@example.com")
        s1 = service.create_session(uid, "k1", "s1")
        s2 = service.create_session(uid, "k2", "s2")
        s3 = service.create_session(uid, "k3", "s3")   # 触发滚动，淘汰最旧 s1
        ids = {s["id"] for s in service.list_sessions(uid)}
        assert s1["id"] not in ids and s2["id"] in ids and s3["id"] in ids
        assert store.qa_session_count(uid) == 2
    finally:
        os.environ.pop("QA_SESSIONS_MAX_PER_USER", None)
    print("[qa] session rolling eviction ok")


# --------------------------------------------------------------------------- #
# 侧边栏聚合
# --------------------------------------------------------------------------- #
def test_sidebar_aggregation():
    uid = _mk_user("sidebar@example.com")
    other = _mk_user("sidebar-other@example.com")
    service.record_action(uid, "https://x.com/s1", "视频1", "summary")
    service.record_action(uid, "https://x.com/s2", "视频2", "transcribe")
    service.create_collection(uid, "侧栏合集")
    service.create_collection(other, "别人的合集")

    data = service.sidebar(uid)
    assert {c["name"] for c in data["collections"]} == {"侧栏合集"}   # 只含本人
    assert {h["title"] for h in data["recent_history"]} == {"视频1", "视频2"}
    print("[sidebar] aggregation isolation ok")


# --------------------------------------------------------------------------- #
# HTTP（TestClient）
# --------------------------------------------------------------------------- #
def test_http_unauthenticated_401():
    """未登录访问任何 /api/me/* 一律 401。"""
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as client:
        assert client.get("/api/me/sidebar").status_code == 401
        assert client.get("/api/me/history").status_code == 401
        assert client.get("/api/me/collections").status_code == 401
        assert client.post("/api/me/collections", json={"name": "x"}).status_code == 401
        assert client.get("/api/me/qa/sessions").status_code == 401
    print("[http] unauthenticated /api/me/* -> 401 ok")


def test_http_me_flow():
    """登录态全链路：注册登录 → 写历史 → 合集 CRUD → 入集 → QA 会话（stub LLM）。"""
    from fastapi.testclient import TestClient

    import backend.library.service as lib_service
    import backend.transcribe as transcribe_mod
    from backend.main import app

    # stub 掉转写与 LLM，纯离线跑通 /api/qa 编排
    orig_transcribe = transcribe_mod.transcribe
    orig_ask = lib_service.ai_qa.ask
    transcribe_mod.transcribe = lambda url, refresh=False: {
        "text": "字幕全文", "title": "HTTP视频", "source": "bilibili",
    }
    lib_service.ai_qa.ask = _AskStub()
    try:
        with TestClient(app) as client:
            r = client.post("/api/auth/register",
                            json={"email": "http@example.com", "password": "password123", "nickname": None})
            assert r.status_code == 201, r.text
            r = client.post("/api/auth/login",
                            json={"identifier": "http@example.com", "password": "password123"})
            assert r.status_code == 200, r.text

            # /api/qa（登录态）：自动建会话并返回 session_id
            url = "https://www.bilibili.com/video/BV1http0001"
            r = client.post("/api/qa", json={"url": url, "question": "讲了什么"})
            assert r.status_code == 200, r.text
            sid = r.json()["session_id"]
            assert sid

            key = transcript_key(url)
            # 历史里出现该视频，且带 qa kind
            r = client.get("/api/me/history")
            assert r.status_code == 200
            row = next(h for h in r.json()["items"] if h["content_key"] == key)
            assert "qa" in row["kinds"] and row["title"] == "HTTP视频"

            # 详情
            assert client.get(f"/api/me/history/{key}").status_code == 200
            assert client.get("/api/me/history/nope").status_code == 404

            # 合集 CRUD + 入集
            r = client.post("/api/me/collections", json={"name": "稍后再看", "description": "d"})
            assert r.status_code == 201, r.text
            cid = r.json()["collection"]["id"]
            r = client.post(f"/api/me/collections/{cid}/items",
                            json={"content_key": key, "url": url, "title": "HTTP视频"})
            assert r.status_code == 201, r.text
            # 重复入集 → 400
            assert client.post(f"/api/me/collections/{cid}/items",
                               json={"content_key": key}).status_code == 400
            r = client.get(f"/api/me/collections/{cid}")
            assert r.status_code == 200 and len(r.json()["items"]) == 1

            # QA 会话列表 / 详情 / 改名
            r = client.get("/api/me/qa/sessions", params={"content_key": key})
            assert r.status_code == 200 and r.json()["sessions"][0]["id"] == sid
            assert client.get(f"/api/me/qa/sessions/{sid}").status_code == 200
            assert client.patch(f"/api/me/qa/sessions/{sid}", json={"title": "改名"}).json()["session"]["title"] == "改名"

            # 侧边栏聚合
            r = client.get("/api/me/sidebar")
            assert r.status_code == 200
            assert any(c["id"] == cid for c in r.json()["collections"])
            assert any(h["content_key"] == key for h in r.json()["recent_history"])

            # 移出条目 / 删合集 / 删会话 / 删历史
            assert client.delete(f"/api/me/collections/{cid}/items/{key}").status_code == 200
            assert client.delete(f"/api/me/collections/{cid}").status_code == 200
            assert client.delete(f"/api/me/qa/sessions/{sid}").status_code == 200
            assert client.delete(f"/api/me/history/{key}").status_code == 200
            assert client.delete(f"/api/me/history/{key}").status_code == 404   # 幂等后再删 → 404
    finally:
        transcribe_mod.transcribe = orig_transcribe
        lib_service.ai_qa.ask = orig_ask
    print("[http] /api/me/* full flow ok")


def test_http_cross_user_404():
    """用户 B 用合法登录态访问用户 A 的私有资源 → 一律 404（不确认存在）。"""
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as ca, TestClient(app) as cb:
        ca.post("/api/auth/register", json={"email": "ua@example.com", "password": "password123", "nickname": None})
        ca.post("/api/auth/login", json={"identifier": "ua@example.com", "password": "password123"})
        cid = ca.post("/api/me/collections", json={"name": "A的合集"}).json()["collection"]["id"]
        key = transcript_key("https://x.com/a-only")
        # 历史无写入端点（由内容端点成功后的 BackgroundTask 写），此处直接经 service 落一条
        service.record_action(storage_user_id(ca), "https://x.com/a-only", "A视频", "summary")

        cb.post("/api/auth/register", json={"email": "ub@example.com", "password": "password123", "nickname": None})
        cb.post("/api/auth/login", json={"identifier": "ub@example.com", "password": "password123"})

        assert cb.get(f"/api/me/collections/{cid}").status_code == 404
        assert cb.delete(f"/api/me/collections/{cid}").status_code == 404
        assert cb.get(f"/api/me/history/{key}").status_code == 404
        assert cb.delete(f"/api/me/history/{key}").status_code == 404
        # B 的侧边栏不含 A 的合集
        assert all(c["id"] != cid for c in cb.get("/api/me/sidebar").json()["collections"])
    print("[http] cross-user 404 isolation ok")


def storage_user_id(client) -> str:
    """从 TestClient 的登录态取回 user_id（读 /api/auth/me）。"""
    return client.get("/api/auth/me").json()["user"]["id"]


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
        # 历史
        test_record_action_aggregates_kinds()
        test_history_isolation_between_users()
        test_history_rolling_eviction()
        test_history_search_and_kind_filter()
        test_history_delete_cascades_qa_sessions()
        test_clear_history()
        # 合集
        test_collections_crud_and_validation()
        test_collections_cap()
        test_collection_items_add_dup_remove()
        test_collection_items_cap()
        test_collection_isolation()
        # 问答会话
        test_qa_session_lifecycle_and_context_assembly()
        test_qa_message_truncation()
        test_qa_session_rolling()
        # 聚合
        test_sidebar_aggregation()
        # HTTP
        test_http_unauthenticated_401()
        test_http_me_flow()
        test_http_cross_user_404()
        print("LIBRARY TEST PASSED")
    finally:
        _cleanup()
