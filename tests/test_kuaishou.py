"""快手专用解析模块（downloader/kuaishou.py）离线测试：不触网。

覆盖：
- can_handle：kuaishou.com / *.kuaishou.com / m.gifshow.com / kwai 命中；不误伤 B站/抖音/视频号
- extract_first_url：分享文案取链
- _photo_id_from_url：/fw/photo/{id}、/short-video/{id} 取 ID
- _parse_fields：mainMvUrls 取视频直链、caption 反转义、duration 毫秒→秒、封面/作者/播放量
- extract_info / download：monkeypatch _fetch_share_page 走完整链路（含流式下载落盘）
- 风控页（无 mainMvUrls）→ KuaishouError
- 门面路由：downloader._pick(快手 URL) 命中 kuaishou（registry 接线正确）

运行：python tests/test_kuaishou.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import downloader  # noqa: E402
from backend.downloader import common, kuaishou  # noqa: E402

# 真实抓取的手机页片段（字段裁剪自 2026-10 实测，结构/键序保持一致）
SAMPLE_HTML = (
    '...<script>var x=1;</script>'
    '{"duration":30000,"timestamp":1768909111354,"likeCount":69720,"width":960,"height":720,'
    '"mainMvUrls":[{"cdn":"v23-3.kwaicdn.com","url":"https://v23-3.kwaicdn.com/upic/a.mp4'
    '?pkey=AAA&clientCacheKey=3xjyi2wunuqu8je_b.mp4"},{"cdn":"x","url":"https://x/b.mp4"}],'
    '"coverUrls":[{"cdn":"p2","url":"https://p2.a.yximgs.com/cover.jpg"}],'
    '"caption":"\\u300c\\u521d\\u5fc3\\u300d 人生\\n成长","user_name":"\\u767d\\u7fbd","viewCount":612013}'
)
FINAL_URL = "https://m.gifshow.com/fw/photo/3xjyi2wunuqu8je?cc=share_wxms"
VIDEO_URL = ("https://v23-3.kwaicdn.com/upic/a.mp4?pkey=AAA&clientCacheKey=3xjyi2wunuqu8je_b.mp4")


def test_detection():
    assert kuaishou.can_handle("https://www.kuaishou.com/f/Xa8oP9pglPbm1P8")
    assert kuaishou.can_handle("https://v.kuaishou.com/abc123")
    assert kuaishou.can_handle("https://m.gifshow.com/fw/photo/3xjy")
    assert kuaishou.can_handle("https://www.kuaishou.com:443/short-video/3xjy")
    assert kuaishou.can_handle("https://kwai.com/photo/x")
    assert not kuaishou.can_handle("https://www.bilibili.com/video/BV1xx")
    assert not kuaishou.can_handle("https://v.douyin.com/abc/")
    assert not kuaishou.can_handle("https://weixin.qq.com/sph/abc")
    assert not kuaishou.can_handle("https://ximalaya.kuaishou-evil.com/")  # 仿冒后缀不误收
    print("[detection] can_handle ok")


def test_extract_first_url():
    text = "看看这个视频 https://www.kuaishou.com/f/Xa8oP9pglPbm1P8 复制此链接打开"
    assert kuaishou.extract_first_url(text) == "https://www.kuaishou.com/f/Xa8oP9pglPbm1P8"
    assert kuaishou.extract_first_url("https://www.kuaishou.com/f/x") == "https://www.kuaishou.com/f/x"
    print("[extract_first_url] ok")


def test_photo_id_from_url():
    assert kuaishou._photo_id_from_url(FINAL_URL) == "3xjyi2wunuqu8je"
    assert kuaishou._photo_id_from_url("https://www.kuaishou.com/short-video/abc123?x=1") == "abc123"
    assert kuaishou._photo_id_from_url("https://www.kuaishou.com/f/token123") is None  # 短链 token 不当作 photoId
    print("[photo_id] extraction ok")


def test_parse_fields():
    f = kuaishou._parse_fields("3xjyi2wunuqu8je", SAMPLE_HTML)
    assert f["video_url"] == VIDEO_URL, f["video_url"]     # 取 mainMvUrls 首元素
    assert f["duration"] == 30.0                             # 30000ms → 30s
    assert f["height"] == 720 and f["width"] == 960
    assert f["view_count"] == 612013
    assert f["thumbnail"] == "https://p2.a.yximgs.com/cover.jpg"
    assert f["title"] == "「初心」 人生\n成长", repr(f["title"])  # \u 与 \n 反转义
    assert f["uploader"] == "白羽"
    assert "3xjyi2wunuqu8je" in f["video_url"]               # clientCacheKey 佐证当前视频
    print("[parse_fields] video/duration/cover/caption/user ok")


def test_extract_info_flow():
    old = kuaishou._fetch_share_page
    kuaishou._fetch_share_page = lambda url: ("3xjyi2wunuqu8je", SAMPLE_HTML, FINAL_URL)
    try:
        info = kuaishou.extract_info("https://www.kuaishou.com/f/Xa8oP9pglPbm1P8")
        assert info["extractor"] == "Kuaishou"
        assert info["id"] == "3xjyi2wunuqu8je"
        assert info["duration"] == 30.0
        assert info["uploader"] == "白羽"
        assert info["webpage_url"] == FINAL_URL
        assert info["formats"][0]["format_id"] == "kuaishou_video"
        assert info["formats"][0]["resolution"] == "720p"
        assert info["ffmpeg_available"] is True
    finally:
        kuaishou._fetch_share_page = old
    print("[extract_info] full flow ok")


def test_download_flow():
    """monkeypatch _session.get：分享页 + 视频流；验证落盘与非空校验。"""
    old_fetch = kuaishou._fetch_share_page
    old_session = kuaishou._session

    class _Resp:
        def __init__(self, text=None, url=FINAL_URL):
            self.text = text
            self.url = url

        def raise_for_status(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_content(self, chunk_size=1):
            return iter([b"\x00\x00\x00 ftypisom", b"x" * 4096])

    class _FakeSession:
        def get(self, url, **kw):
            if kw.get("stream"):
                return _Resp()  # 视频流
            return _Resp(text=SAMPLE_HTML, url=FINAL_URL)

    kuaishou._fetch_share_page = lambda url: ("3xjyi2wunuqu8je", SAMPLE_HTML, FINAL_URL)
    kuaishou._session = _FakeSession()
    try:
        out = kuaishou.download("https://www.kuaishou.com/f/Xa8oP9pglPbm1P8")
        p = Path(out["filepath"])
        assert p.exists() and out["filesize"] > 0
        assert out["filename"].endswith(".mp4")
        assert "白羽" in out["title"] or out["title"]
        p.unlink(missing_ok=True)  # 清理测试产物
    finally:
        kuaishou._fetch_share_page = old_fetch
        kuaishou._session = old_session
    print("[download] stream to disk ok")


def test_risk_control_page():
    """终点页无 mainMvUrls（风控/删除）→ KuaishouError（中文，交上层按 400）。"""
    old_session = kuaishou._session

    class _Resp:
        text = "<html>请在快手App打开验证</html>"
        url = "https://m.gifshow.com/fw/photo/deleted123"

        def raise_for_status(self):
            pass

    class _FakeSession:
        def get(self, url, **kw):
            return _Resp()

    kuaishou._session = _FakeSession()
    try:
        raised = False
        try:
            kuaishou._fetch_share_page("https://www.kuaishou.com/f/x")
        except kuaishou.KuaishouError as exc:
            raised = True
            assert "解析" in str(exc) or "风控" in str(exc)
        assert raised, "无 mainMvUrls 应抛 KuaishouError"
    finally:
        kuaishou._session = old_session
    print("[risk-control] no data → KuaishouError ok")


def test_facade_routing():
    """门面 _pick：快手 URL 命中 kuaishou，其余不误伤。"""
    assert downloader._pick("https://www.kuaishou.com/f/Xa8oP9pglPbm1P8") is kuaishou
    assert downloader._pick("https://m.gifshow.com/fw/photo/abc") is kuaishou
    assert downloader._pick("https://v.douyin.com/abc/").__name__.endswith("douyin")
    assert downloader._pick("https://weixin.qq.com/sph/abc").__name__.endswith("wechat_channels")
    print("[routing] facade _pick ok")


if __name__ == "__main__":
    assert common.DOWNLOAD_DIR.exists()  # 下载目录就绪
    test_detection()
    test_extract_first_url()
    test_photo_id_from_url()
    test_parse_fields()
    test_extract_info_flow()
    test_download_flow()
    test_risk_control_page()
    test_facade_routing()
    print("KUAISHOU TEST PASSED")
