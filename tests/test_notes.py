"""随手笔记（笔记 / 图片 / 抽帧）的离线测试（不触网、不需真实 Key）。

覆盖：
- service：marks 解析（去重/升序/多格式）、创建即写 note 徽标、删光笔记移除徽标、
           正文截断、乐观并发 409（server 版本随行）、超限滚动淘汰、跨用户列级隔离
- images：魔数校验（拒伪造/SVG）、落盘与读取、删除幂等、孤儿图片认领与超龄惰性清扫
- frames：SSRF 防线（内网/回环/保留 IP、localhost 域名解析、非 http 协议）、
           时间越界、无缓存无封面 → ValidationError（stub 掉 yt-dlp 封面兜底，不触网）
- router（TestClient）：未登录 401、登录态全链路（CRUD / 传图 / 读图 / 删图 /
           409 冲突响应携带 server）、跨用户访问一律 404

运行：python tests/test_notes.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 将项目根目录加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 关键：db.py 在「导入时」即按 DATABASE_URL 建 engine，必须在导入任何 backend.* 之前设置。
_TMP_DIR = Path(tempfile.mkdtemp(prefix="mp_notes_test_"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP_DIR / 'test_notes.db').as_posix()}"
os.environ["NOTE_IMAGE_DIR"] = (_TMP_DIR / "note_images").as_posix()
os.environ["AUTH_ENABLED"] = "true"
os.environ["AUTH_ALLOW_ANONYMOUS"] = "false"
os.environ["LOGIN_MAX_ATTEMPTS"] = "100"
os.environ["LOGIN_IP_MAX_PER_WINDOW"] = "100000"   # 功能测试默认不受 IP 限流干扰
os.environ["PASSWORD_MIN_LENGTH"] = "8"
os.environ["PHONE_DEFAULT_REGION"] = "CN"

from backend import storage  # noqa: E402
from backend.auth import service as auth_service  # noqa: E402
from backend.library import service as lib_service  # noqa: E402
from backend.notes import config, frames, images, service, store  # noqa: E402
from backend.notes.errors import (  # noqa: E402
    ConflictError,
    NotFoundError,
    ValidationError,
)
from backend.storage import models, transcript_key  # noqa: E402
from backend.storage.db import session as db_session  # noqa: E402

# 最小可过魔数校验的 PNG（sniff 只看文件头，body 任意）
FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
FAKE_JPG = b"\xff\xd8\xff" + b"\x00" * 64


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
# marks 解析
# --------------------------------------------------------------------------- #
def test_parse_marks():
    """时间戳标记解析：多格式、去重（同 t 只留首个）、按时间升序。"""
    body = (
        "开头 @[03:12](t=192)\n"
        "重复时间 @[03:12](t=192) 不重复计入\n"
        "更早 @[00:05](t=5)\n"
        "小时制 @[1:02:03](t=3723)\n"
        "非标记文本 [03:12](t=1) 与 @ 03:12 不匹配\n"
    )
    marks = service.parse_marks(body)
    assert [m["t"] for m in marks] == [5, 192, 3723]      # 升序 + 去重
    assert marks[0]["label"] == "00:05" and marks[2]["label"] == "1:02:03"
    assert service.parse_marks("没有任何标记的正文") == []
    assert service.parse_marks("") == []
    print("[marks] parse / dedup / sort ok")


# --------------------------------------------------------------------------- #
# 笔记 CRUD 与历史徽标联动
# --------------------------------------------------------------------------- #
def test_note_crud_and_kind_sync():
    """创建（带 url）写 note 徽标；更新重解析 marks；删光该视频笔记移除徽标。"""
    uid = _mk_user("crud@example.com")
    url = "https://www.bilibili.com/video/BV1note0001"
    key = transcript_key(url)

    note = service.create_note(uid, key, "看 @[03:12](t=192) 处", url=url, title="笔记视频")
    assert note["content_key"] == key
    assert note["marks"] == [{"t": 192.0, "label": "03:12"}]
    assert "note" in lib_service.store.history_get(uid, key)["kinds"]   # 徽标已写

    # 更新：marks 重解析、title/starred 独立更新
    updated = service.update_note(uid, note["id"], body="改到 @[00:10](t=10) 与 @[05:00](t=300)")
    assert [m["t"] for m in updated["marks"]] == [10, 300]
    assert service.update_note(uid, note["id"], starred=True)["starred"] is True

    # 同视频第二篇：徽标幂等
    n2 = service.create_note(uid, key, "第二篇", url=url)
    assert "note" in lib_service.store.history_get(uid, key)["kinds"]

    # 删光两篇 → 徽标移除
    service.delete_note(uid, note["id"])
    assert "note" in lib_service.store.history_get(uid, key)["kinds"]    # 还剩一篇
    service.delete_note(uid, n2["id"])
    assert "note" not in lib_service.store.history_get(uid, key)["kinds"]
    _raises(NotFoundError, service.delete_note, uid, n2["id"])          # 再删 → NotFound
    print("[note] crud / marks reparse / kind sync ok")


def test_note_body_truncation():
    """正文超限：容忍截断（不报错），返回 truncated=True。"""
    os.environ["NOTE_MAX_CHARS"] = "10"
    try:
        uid = _mk_user("trunc@example.com")
        note = service.create_note(uid, "k_trunc", "甲" * 25)
        assert len(note["body"]) == 10 and note["truncated"] is True
        updated = service.update_note(uid, note["id"], body="乙" * 25)
        assert len(updated["body"]) == 10 and updated["truncated"] is True
    finally:
        os.environ.pop("NOTE_MAX_CHARS", None)
    print("[note] body truncation ok")


def test_update_conflict_409():
    """乐观并发：库中版本新于 base_updated_at → ConflictError（携带服务端版本）；缺省跳过检测。"""
    uid = _mk_user("conflict@example.com")
    note = service.create_note(uid, "k_conflict", "初稿")
    v1 = note["updated_at"]

    # 另一窗口先保存（推进服务端版本）
    v2 = service.update_note(uid, note["id"], body="另一窗口的修改")["updated_at"]

    # 旧基线再保存 → 409，server 为完整服务端版本
    _raises(ConflictError, service.update_note, uid, note["id"], body="我的修改", base_updated_at=v1)
    try:
        service.update_note(uid, note["id"], body="我的修改", base_updated_at=v1)
    except ConflictError as exc:
        assert exc.server["body"] == "另一窗口的修改"
        assert exc.server["updated_at"] == v2

    # 以服务端版本为基线覆盖写入 → 成功（「用我的版本覆盖」通道）
    merged = service.update_note(uid, note["id"], body="我的修改", base_updated_at=v2)
    assert merged["body"] == "我的修改"

    # base 缺省（老客户端/脚本）→ 跳过检测直接写（向后兼容）
    assert service.update_note(uid, note["id"], body=None, starred=True)["starred"] is True
    print("[note] optimistic conflict 409 / server payload / overwrite ok")


def test_notes_rolling_eviction():
    """超限滚动淘汰最旧不活跃（淘汰级联其图片）。"""
    os.environ["NOTES_MAX_PER_USER"] = "2"
    try:
        uid = _mk_user("roll@example.com")
        n1 = service.create_note(uid, "k1", "第一篇（最旧）")
        n2 = service.create_note(uid, "k2", "第二篇")
        # 给 n2 一张图：淘汰 n1 时不影响它；随后 n3 触发淘汰 n1（n1 无图只验证行为）
        service.upload_image(uid, FAKE_PNG, "k2", note_id=n2["id"])
        n3 = service.create_note(uid, "k3", "第三篇")   # 触发滚动，淘汰 n1
        ids = {n["id"] for n in service.list_notes(uid)["items"]}
        assert n1["id"] not in ids and n2["id"] in ids and n3["id"] in ids
        assert service.get_note(uid, n2["id"])["image_count"] == 1   # 幸存者图片完好
    finally:
        os.environ.pop("NOTES_MAX_PER_USER", None)
    print("[note] rolling eviction ok")


def test_note_isolation():
    """列级隔离：A 的笔记 B 看不到、改不到、删不到。"""
    a = _mk_user("isoa@example.com")
    b = _mk_user("isob@example.com")
    note = service.create_note(a, "k_iso", "A 的笔记")
    assert service.list_notes(b, content_key="k_iso")["total"] == 0
    _raises(NotFoundError, service.get_note, b, note["id"])
    _raises(NotFoundError, service.update_note, b, note["id"], body="劫持")
    _raises(NotFoundError, service.delete_note, b, note["id"])
    print("[note] cross-user isolation ok")


# --------------------------------------------------------------------------- #
# 图片：魔数 / 落盘 / 认领 / 清扫
# --------------------------------------------------------------------------- #
def test_image_sniff_rejects_fake_and_svg():
    """魔数校验：伪造 png 后缀的文本、SVG（可内嵌脚本）一律拒绝。"""
    uid = _mk_user("sniff@example.com")
    _raises(ValidationError, service.upload_image, uid, b"not an image at all", "k_sniff")
    _raises(ValidationError, service.upload_image, uid,
            b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "k_sniff")
    # 真位图通过
    img = service.upload_image(uid, FAKE_PNG, "k_sniff")
    assert img["mime"] == "image/png" and img["url"].endswith(img["id"])
    # 体积上限
    os.environ["NOTE_IMAGE_MAX_BYTES"] = "16"
    try:
        _raises(ValidationError, service.upload_image, uid, FAKE_PNG, "k_sniff")
    finally:
        os.environ.pop("NOTE_IMAGE_MAX_BYTES", None)
    print("[image] magic sniff / svg reject / size limit ok")


def test_image_lifecycle():
    """上传 → 读取（字节一致）→ 计数维护 → 删除（行+文件，幂等）。"""
    uid = _mk_user("life@example.com")
    note = service.create_note(uid, "k_life", "带图笔记")
    img = service.upload_image(uid, FAKE_JPG, "k_life", t=12.5, note_id=note["id"])
    assert service.get_note(uid, note["id"])["image_count"] == 1
    assert len(service.get_note(uid, note["id"])["images"]) == 1

    found = service.get_image_content(uid, img["id"])
    assert found is not None and found[0] == FAKE_JPG and found[1] == "image/jpeg"

    service.delete_image(uid, img["id"])
    assert service.get_image_content(uid, img["id"]) is None           # 文件已删
    assert service.get_note(uid, note["id"])["image_count"] == 0       # 计数回写
    _raises(NotFoundError, service.delete_image, uid, img["id"])       # 再删 → NotFound

    # 删笔记级联删其图片行与文件
    img2 = service.upload_image(uid, FAKE_PNG, "k_life", note_id=note["id"])
    service.delete_note(uid, note["id"])
    assert service.get_image_content(uid, img2["id"]) is None
    print("[image] lifecycle / count / cascade delete ok")


def test_orphan_claim_and_sweep():
    """孤儿图片：新建未保存先贴图 → create_note 按 content_key 认领；超龄孤儿惰性清扫。"""
    uid = _mk_user("orphan@example.com")
    key = "k_orphan"
    note0 = service.create_note(uid, key, "既有笔记")
    # 新建未保存时贴图（无 note_id）——注意 create_note 会认领当前全部孤儿，
    # 故孤儿必须在既有笔记创建之后才上传
    orphan = service.upload_image(uid, FAKE_PNG, key, t=8)
    # 同视频另一张直接归属既有笔记的图
    owned = service.upload_image(uid, FAKE_PNG, key, note_id=note0["id"])

    # 建新笔记：认领该视频全部孤儿（owned 已有 note_id，不在认领范围）
    note = service.create_note(uid, key, "新笔记认领孤儿")
    assert note["images_claimed"] == 1
    assert store.image_get(uid, orphan["id"])["note_id"] == note["id"]
    assert service.get_note(uid, note["id"])["image_count"] == 1

    # 超龄孤儿清扫：手动把一张新孤儿行的 created_at 改到 2 天前，触发写路径清扫
    stale = service.upload_image(uid, FAKE_PNG, key)   # 新孤儿（无 note_id）
    with db_session() as s:
        row = s.get(models.NoteImage, stale["id"])
        row.created_at = datetime.now(timezone.utc) - timedelta(days=2)
    service._sweep_orphan_images(uid)
    assert store.image_get(uid, stale["id"]) is None                  # 行已清
    assert service.get_image_content(uid, stale["id"]) is None        # 文件已清
    assert store.image_get(uid, owned["id"]) is not None              # 已认领的不受影响
    print("[image] orphan claim / ttl sweep ok")


# --------------------------------------------------------------------------- #
# 抽帧：SSRF 防线与离线降级（stub 掉 yt-dlp，不触网）
# --------------------------------------------------------------------------- #
def test_frames_url_validation():
    """SSRF 防线：内网/回环/保留字面 IP、localhost 域名解析、非 http 协议一律拒绝。"""
    for bad in (
        "http://127.0.0.1/x",           # 回环
        "https://192.168.1.1/v",        # 私网
        "http://10.0.0.5/v",            # 私网
        "http://169.254.169.254/meta",  # 链路本地（云元数据端点）
        "http://localhost/v",           # 域名解析回环（覆盖 getaddrinfo 分支）
        "ftp://example.com/v",          # 非 http 协议
        "file:///etc/passwd",
        "https:///no-host",
        "   ",
    ):
        _raises(ValidationError, frames.validate_frame_url, bad)
    assert frames.validate_frame_url("https://www.bilibili.com/video/BV1okok") == \
        "https://www.bilibili.com/video/BV1okok"
    print("[frames] ssrf validation ok")


def test_frames_capture_offline():
    """capture 离线行为：时间越界拒绝；无缓存且远程/封面兜底均失败 → ValidationError。

    extract_frame_remote / extract_poster 全部 stub：不触网（yt-dlp/ffmpeg 都不跑）。
    """
    orig = frames.extract_poster
    orig_remote = frames.extract_frame_remote
    frames.extract_poster = lambda url: None   # stub：跳过 yt-dlp，不触网
    frames.extract_frame_remote = lambda url, t: None   # stub：跳过远程抽帧
    try:
        _raises(ValidationError, frames.capture, "https://example.com/v", -1, None)
        _raises(ValidationError, frames.capture, "https://example.com/v", 24 * 3600 + 1, None)
        # 无缓存 + 远程/封面均失败 → 明确报错（绝不触发整片下载）
        _raises(ValidationError, frames.capture, "https://example.com/v", 10, None)
        # 远程直连抽帧成功（iframe 平台主通道）→ frame 而非 poster
        frames.extract_frame_remote = lambda url, t: FAKE_PNG
        shot = frames.capture("https://example.com/v", 10, None)
        assert shot["kind"] == "frame" and shot["data"] == FAKE_PNG
        frames.extract_frame_remote = lambda url, t: None
        # 封面兜底成功（返回位图）→ poster 通道
        frames.extract_poster = lambda url: FAKE_PNG
        shot = frames.capture("https://example.com/v", 10, None)
        assert shot["kind"] == "poster" and shot["data"] == FAKE_PNG
        # SSRF 在 capture 编排内同样拦截（校验先于一切网络动作）
        frames.extract_poster = lambda url: FAKE_PNG
        _raises(ValidationError, frames.capture, "http://192.168.0.1/v", 0, None)
    finally:
        frames.extract_frame_remote = orig_remote
        frames.extract_poster = orig
    print("[frames] capture offline / remote / poster fallback ok")


# --------------------------------------------------------------------------- #
# HTTP（TestClient）
# --------------------------------------------------------------------------- #
def test_http_unauthenticated_401():
    """未登录访问任何 /api/me/notes* 一律 401。"""
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as client:
        assert client.get("/api/me/notes").status_code == 401
        assert client.post("/api/me/notes", json={"content_key": "k"}).status_code == 401
        assert client.get("/api/me/notes/note123").status_code == 401
        assert client.get("/api/me/notes/images/img123").status_code == 401
        assert client.post("/api/me/notes/frames", json={"url": "https://x.com/v", "t": 0}).status_code == 401
    print("[http] unauthenticated /api/me/notes* -> 401 ok")


def test_http_notes_flow():
    """登录态全链路：建 → 列表/详情 → 自动保存 + 409 → 传图/读图/删图 → 删笔记。"""
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as client:
        client.post("/api/auth/register",
                    json={"email": "httpnote@example.com", "password": "password123", "nickname": None})
        client.post("/api/auth/login",
                    json={"identifier": "httpnote@example.com", "password": "password123"})

        url = "https://www.bilibili.com/video/BV1httpnote"
        key = transcript_key(url)

        # 建笔记：marks 服务端解析
        r = client.post("/api/me/notes", json={
            "content_key": key, "url": url, "title": "HTTP笔记视频", "body": "看到 @[03:12](t=192)",
        })
        assert r.status_code == 201, r.text
        note = r.json()["note"]
        assert note["marks"] == [{"t": 192.0, "label": "03:12"}]

        # 历史出现该视频且带 note 徽标
        hist = client.get("/api/me/history").json()
        row = next(h for h in hist["items"] if h["content_key"] == key)
        assert "note" in row["kinds"]

        # 列表（content_key 过滤）与详情
        r = client.get("/api/me/notes", params={"content_key": key})
        assert r.status_code == 200 and r.json()["total"] == 1
        assert client.get(f"/api/me/notes/{note['id']}").json()["note"]["body"].startswith("看到")

        # 自动保存：PATCH 带 base_updated_at 基线
        r = client.patch(f"/api/me/notes/{note['id']}",
                         json={"body": "更新 @[00:05](t=5)", "base_updated_at": note["updated_at"]})
        assert r.status_code == 200, r.text
        v2 = r.json()["note"]
        assert [m["t"] for m in v2["marks"]] == [5]

        # 旧基线再 PATCH → 409 + server（前端二选一依据）
        r = client.patch(f"/api/me/notes/{note['id']}",
                         json={"body": "冲突写入", "base_updated_at": note["updated_at"]})
        assert r.status_code == 409, r.text
        assert r.json()["server"]["body"] == "更新 @[00:05](t=5)"

        # 传图（multipart，魔数校验）→ 读图（字节一致 + 私有缓存头）→ 删图
        r = client.post("/api/me/notes/images",
                        files={"file": ("shot.png", FAKE_PNG, "image/png")},
                        data={"content_key": key, "note_id": note["id"], "t": "192"})
        assert r.status_code == 201, r.text
        img = r.json()["image"]
        r = client.get(img["url"])
        assert r.status_code == 200 and r.content == FAKE_PNG
        assert "immutable" in r.headers.get("cache-control", "")
        assert client.patch(f"/api/me/notes/{note['id']}", json={}).json()["note"]["image_count"] == 1

        # 伪造图片（文本）→ 400
        assert client.post("/api/me/notes/images",
                           files={"file": ("fake.png", b"plain text", "image/png")},
                           data={"content_key": key}).status_code == 400

        assert client.delete(img["url"]).status_code == 200
        assert client.get(img["url"]).status_code == 404

        # 删笔记 → 404 幂等；历史 note 徽标移除
        assert client.delete(f"/api/me/notes/{note['id']}").status_code == 200
        assert client.delete(f"/api/me/notes/{note['id']}").status_code == 404
        hist = client.get("/api/me/history").json()
        row = next(h for h in hist["items"] if h["content_key"] == key)
        assert "note" not in row["kinds"]
    print("[http] notes full flow ok")


def test_http_cross_user_404():
    """用户 B 用合法登录态访问 A 的笔记/图片 → 一律 404（不确认存在）。"""
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as ca, TestClient(app) as cb:
        ca.post("/api/auth/register", json={"email": "na@example.com", "password": "password123", "nickname": None})
        ca.post("/api/auth/login", json={"identifier": "na@example.com", "password": "password123"})
        note = ca.post("/api/me/notes", json={"content_key": "k_a_only", "body": "A的笔记"}).json()["note"]
        img = ca.post("/api/me/notes/images",
                      files={"file": ("shot.png", FAKE_PNG, "image/png")},
                      data={"content_key": "k_a_only", "note_id": note["id"]}).json()["image"]

        cb.post("/api/auth/register", json={"email": "nb@example.com", "password": "password123", "nickname": None})
        cb.post("/api/auth/login", json={"identifier": "nb@example.com", "password": "password123"})

        assert cb.get(f"/api/me/notes/{note['id']}").status_code == 404
        assert cb.patch(f"/api/me/notes/{note['id']}", json={"body": "劫持"}).status_code == 404
        assert cb.delete(f"/api/me/notes/{note['id']}").status_code == 404
        assert cb.get(img["url"]).status_code == 404
        assert cb.delete(img["url"]).status_code == 404
    print("[http] cross-user 404 isolation ok")


def test_http_frame_endpoint():
    """抽帧端点：SSRF 拒绝 400；无缓存且远程/封面均失败 → 400 提示（全 stub 不触网）。

    不介入 _stream_cache_lookup：main.py 注入的真实回调对空流播缓存返回 None，
    测试环境无缓存，自然走远程抽帧→封面兜底分支（且避开 backend.notes.router 被
    __init__ 的 router 属性遮蔽、import as 拿不到模块的问题）。
    """
    from fastapi.testclient import TestClient

    from backend.main import app

    orig_poster = frames.extract_poster
    orig_remote = frames.extract_frame_remote
    frames.extract_poster = lambda url: None
    frames.extract_frame_remote = lambda url, t: None
    try:
        with TestClient(app) as client:
            client.post("/api/auth/register",
                        json={"email": "frame@example.com", "password": "password123", "nickname": None})
            client.post("/api/auth/login",
                        json={"identifier": "frame@example.com", "password": "password123"})

            # SSRF：内网地址在远程抽帧/封面兜底之前就被拦截
            r = client.post("/api/me/notes/frames", json={"url": "http://192.168.1.1/v", "t": 0})
            assert r.status_code == 400

            # 公网地址：无缓存 + 远程/封面均失败 → 400 明确提示（不触发整片下载）
            r = client.post("/api/me/notes/frames", json={"url": "https://example.com/v", "t": 30})
            assert r.status_code == 400 and "重试" in r.json()["detail"]

            # 远程直连抽帧成功（B站/YouTube iframe 主通道）→ 201 + frame_type=frame
            frames.extract_frame_remote = lambda url, t: FAKE_PNG
            r = client.post("/api/me/notes/frames", json={"url": "https://example.com/v", "t": 30})
            assert r.status_code == 201, r.text
            data = r.json()
            assert data["frame_type"] == "frame"
            assert client.get(data["image"]["url"]).status_code == 200
            frames.extract_frame_remote = lambda url, t: None

            # 封面兜底成功 → 201 + frame_type=poster + 图片可直接读取
            frames.extract_poster = lambda url: FAKE_PNG
            r = client.post("/api/me/notes/frames", json={"url": "https://example.com/v", "t": 30})
            assert r.status_code == 201, r.text
            data = r.json()
            assert data["frame_type"] == "poster"
            assert client.get(data["image"]["url"]).status_code == 200
    finally:
        frames.extract_frame_remote = orig_remote
        frames.extract_poster = orig_poster
    print("[http] frame endpoint / ssrf / remote + poster fallback ok")


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
        # marks / 笔记
        test_parse_marks()
        test_note_crud_and_kind_sync()
        test_note_body_truncation()
        test_update_conflict_409()
        test_notes_rolling_eviction()
        test_note_isolation()
        # 图片
        test_image_sniff_rejects_fake_and_svg()
        test_image_lifecycle()
        test_orphan_claim_and_sweep()
        # 抽帧
        test_frames_url_validation()
        test_frames_capture_offline()
        # HTTP
        test_http_unauthenticated_401()
        test_http_notes_flow()
        test_http_cross_user_404()
        test_http_frame_endpoint()
        print("NOTES TEST PASSED")
    finally:
        _cleanup()
