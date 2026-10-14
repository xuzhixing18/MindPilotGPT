"""快手评论（comments/kuaishou.py）+ 音频路由（transcribe/audio.py）离线测试：不触网。

覆盖：
- can_handle：kuaishou/gifshow/kwai 命中；douyin/ximalaya/仿冒后缀不误收
- comments._pick 路由：快手域名命中专用模块（不回退 generic）
- fetch：monkeypatch resolve_photo_id + _new_session + _fetch_page；
  字段归一（毫秒→秒、realLikedCount 优先）、commentId 去重、按 limit/pcursor/noMore 停翻页
- GraphQL errors / HTTP 非 200 → CommentsError（不伪装「不支持」）；确无评论 → 空列表
- 门面集成：空评论经 fetch_comments 抛 CommentsNotSupportedError
- audio.acquire_audio 路由：注册表命中专用解析器走 _specific_audio，否则 _ytdlp_audio

运行：python tests/test_kuaishou_comments.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# db.py 导入即按 DATABASE_URL 建 engine，故须在导入 backend.* 前指向临时库
_TMP_DIR = Path(tempfile.mkdtemp(prefix="mp_ks_test_"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP_DIR / 'test_ks.db').as_posix()}"
os.environ.pop("COMMENTS_CACHE_HOURS", None)

from backend import comments, storage  # noqa: E402
from backend.comments import kuaishou  # noqa: E402
from backend.comments.errors import CommentsError, CommentsNotSupportedError  # noqa: E402
from backend.downloader import kuaishou as ks_dl  # noqa: E402
from backend.transcribe import audio  # noqa: E402

storage.init_db()

KS_URL = "https://www.kuaishou.com/f/Xa8oP9pglPbm1P8"


def _c(cid, text, likes, author="a", ts=None):
    return {
        "commentId": cid, "content": text, "authorName": author,
        "realLikedCount": likes, "likedCount": str(likes),
        "timestamp": ts if ts is not None else 1769006277851,  # 毫秒
    }


def _patch(photo_id="3xjyi2wunuqu8je", title="快手视频", pages=None):
    """monkeypatch 三处网络点：resolve_photo_id / _new_session / _fetch_page。

    pages: {pcursor_in -> node_out}；node 形如 {rootComments, pcursor}。
    """
    seq: list[str] = []
    nodes = pages or {}

    ks_dl.resolve_photo_id = lambda url: (photo_id, title)
    kuaishou._new_session = lambda: object()

    def fake_fetch_page(session, pid, pcursor):
        assert pid == photo_id
        seq.append(pcursor)
        node = nodes.get(pcursor, {"rootComments": [], "pcursor": ""})
        if isinstance(node, Exception):
            raise node
        return node

    kuaishou._fetch_page = fake_fetch_page
    return seq


def _restore():
    del kuaishou._new_session
    del kuaishou._fetch_page
    del ks_dl.resolve_photo_id


def test_can_handle():
    assert kuaishou.can_handle(KS_URL)
    assert kuaishou.can_handle("https://v.kuaishou.com/abc")
    assert kuaishou.can_handle("https://m.gifshow.com/fw/photo/xyz")
    assert kuaishou.can_handle("https://www.kwai.com/short-video/1")
    assert not kuaishou.can_handle("https://www.douyin.com/video/1")
    assert not kuaishou.can_handle("https://www.ximalaya.com/sound/1")
    assert not kuaishou.can_handle("https://ximalaya.kuaishou-evil.com/f/1")  # 仿冒后缀
    print("[can_handle] detect ok")


def test_routing():
    assert comments._pick(KS_URL).__name__.endswith("kuaishou")
    assert comments._pick("https://www.bilibili.com/video/BV1xx").__name__.endswith("bilibili")
    print("[routing] comments._pick ok")


def test_fetch_flow():
    """翻页 + 去重 + 毫秒转秒 + realLikedCount 优先 + limit 达标即停。"""
    p1 = {"rootComments": [_c(1, "高赞", 1582), _c(2, "  ", 999), _c(3, "中赞", 30, ts=1769000000000)], "pcursor": "P2"}
    p2 = {"rootComments": [_c(1, "高赞", 1582), _c(4, "第四条", 5)], "pcursor": "noMore"}
    seq = _patch(pages={"": p1, "P2": p2})
    try:
        out = kuaishou.fetch(KS_URL, 40)
        texts = [c["text"] for c in out["comments"]]
        assert texts == ["高赞", "中赞", "第四条"], texts  # 空文本过滤 + cid 去重
        assert out["comments"][0]["likes"] == 1582
        assert out["comments"][1]["time"] == 1769000000  # 毫秒 → 秒
        assert seq == ["", "P2"], seq  # 用 pcursor 前进，noMore 停止
    finally:
        _restore()
    print("[fetch] paging / dedup / ms->sec ok")


def test_single_page_when_limit_met():
    """limit 小且首页已足量：只请求 1 页。"""
    p1 = {"rootComments": [_c(i, f"评{i}", i) for i in range(25)], "pcursor": "P2"}
    seq = _patch(pages={"": p1, "P2": {"rootComments": [_c(99, "不该到", 1)], "pcursor": ""}})
    try:
        out = kuaishou.fetch(KS_URL, 20)
        assert len(out["comments"]) == 25
        assert seq == [""], f"limit 达标应立即停，实际翻页 {seq}"
    finally:
        _restore()
    print("[fetch.limit] 首页足量单页 ok")


def test_error_classification():
    """GraphQL/HTTP 异常经 _fetch_page 抛 CommentsError 透传；无 errors 且空评论 → 空列表。"""
    _patch(pages={"": CommentsError("快手评论接口返回 HTTP 403（可能被风控）。")})
    raised = None
    try:
        kuaishou.fetch(KS_URL, 10)
    except CommentsNotSupportedError:
        raised = "not_supported"
    except CommentsError:
        raised = "error"
    finally:
        _restore()
    assert raised == "error", "接口异常应抛 CommentsError，不得伪装 NotSupported"

    _patch(pages={"": {"rootComments": [], "pcursor": ""}})
    try:
        assert kuaishou.fetch(KS_URL, 10)["comments"] == []
    finally:
        _restore()
    print("[errors] CommentsError 透传 / 空评论 ok")


def test_facade_not_supported():
    """门面集成：真无评论时 fetch_comments 抛 CommentsNotSupportedError。"""
    _patch(pages={"": {"rootComments": [], "pcursor": ""}})
    try:
        raised = False
        try:
            comments.fetch_comments(KS_URL, refresh=True)
        except CommentsNotSupportedError:
            raised = True
        assert raised, "空评论应经门面抛 CommentsNotSupportedError"
    finally:
        _restore()
    print("[facade] not-supported ok")


def test_audio_routing():
    """acquire_audio：注册表命中专用解析器走 _specific_audio，否则 _ytdlp_audio。"""
    called: list[str] = []
    orig_spec, orig_yt = audio._specific_audio, audio._ytdlp_audio

    def fake_spec(mod, url):
        called.append(f"specific:{mod.__name__.rsplit('.', 1)[-1]}")
        return Path("x.mp3"), "t"

    def fake_yt(url):
        called.append("ytdlp")
        return Path("x.mp3"), "t"

    audio._specific_audio = fake_spec
    audio._ytdlp_audio = fake_yt
    try:
        audio.acquire_audio(KS_URL)                 # 快手 → 专用
        audio.acquire_audio("https://www.douyin.com/video/1")  # 抖音 → 专用
        audio.acquire_audio("https://www.bilibili.com/video/BV1xx")  # B站 → generic → ytdlp
    finally:
        audio._specific_audio, audio._ytdlp_audio = orig_spec, orig_yt
    assert called == ["specific:kuaishou", "specific:douyin", "ytdlp"], called
    print("[audio] acquire_audio routing ok")


if __name__ == "__main__":
    test_can_handle()
    test_routing()
    test_fetch_flow()
    test_single_page_when_limit_met()
    test_error_classification()
    test_facade_not_supported()
    test_audio_routing()
    print("KUAISHOU COMMENTS+AUDIO TEST PASSED")
