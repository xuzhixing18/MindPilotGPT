"""喜马拉雅评论抓取（comments/ximalaya.py）离线测试：不触网、无 DB 依赖。

覆盖：
- _extract_ids：sound/album 链接提取；无 ID 链接抛 CommentsError
- _pick 路由：ximalaya 域名命中专用模块（不回退 generic）
- fetch(sound)：track→baseInfo→albumId→评论接口；字段归一（毫秒→秒）、commentId 去重
- 整页重复立即停止翻页（服务端忽略 pageId 时不浪费请求）；limit≤20 只请求 1 页
- fetch(album)：album 链接跳过 baseInfo 直达评论接口
- code!=0 → CommentsError（不伪装「不支持」，与 bilibili 同红线）；code=0 空评论 → 空列表
- 门面集成：空评论经 fetch_comments 抛 CommentsNotSupportedError

运行：python tests/test_ximalaya.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# db.py 导入即按 DATABASE_URL 建 engine，故须在导入 backend.* 前指向临时库
_TMP_DIR = Path(tempfile.mkdtemp(prefix="mp_xima_test_"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP_DIR / 'test_xima.db').as_posix()}"
os.environ.pop("COMMENTS_CACHE_HOURS", None)

from backend import comments, storage  # noqa: E402
from backend.comments import ximalaya  # noqa: E402
from backend.comments.errors import CommentsError, CommentsNotSupportedError  # noqa: E402

storage.init_db()

TRACK_URL = "https://www.ximalaya.com/sound/303058616"
ALBUM_URL = "https://www.ximalaya.com/album/38416783"


def _c(cid, text, likes, nick="n", created=None):
    return {
        "commentId": cid, "content": text, "likes": likes, "nickname": nick,
        "createdAt": created if created is not None else 1779245119000,  # 毫秒
    }


def _patch(routes):
    """monkeypatch _get_json：按 URL 关键字路由假响应，并记录调用序列。"""
    calls: list[tuple[str, dict]] = []

    def fake_get_json(url, params=None):
        calls.append((url, dict(params or {})))
        for kw, resp in routes.items():
            if kw in url:
                if callable(resp):
                    return resp(params or {})
                return resp
        raise AssertionError(f"unexpected url: {url}")

    ximalaya._get_json = fake_get_json
    return calls


def _restore():
    del ximalaya._get_json  # 回到模块真实实现


def test_extract_ids():
    assert ximalaya._extract_ids(TRACK_URL) == (303058616, None)
    assert ximalaya._extract_ids(ALBUM_URL) == (None, 38416783)
    raised = False
    try:
        ximalaya._extract_ids("https://www.ximalaya.com/")
    except CommentsError:
        raised = True
    assert raised, "无法识别 ID 应抛 CommentsError"
    print("[extract_ids] sound/album/invalid ok")


def test_routing():
    assert comments._pick(TRACK_URL).__name__.endswith("ximalaya")
    assert comments._pick("https://www.bilibili.com/video/BV1xx").__name__.endswith("bilibili")
    assert comments._pick("https://www.youtube.com/watch?v=abc").__name__.endswith("generic")
    print("[routing] _pick ok")


def test_fetch_sound_flow():
    """sound 链接：baseInfo→albumId→评论接口；归一化+去重+毫秒转秒；整页重复即停。"""
    def _comments(params):
        page = int(params.get("pageId") or 1)
        if page == 1:
            batch = [_c(1, "高赞", 68), _c(2, "   ", 999), _c(3, "中赞", 30, created=1700000000000)]
        elif page == 2:
            batch = [_c(1, "高赞", 68), _c(3, "中赞", 30)]  # 整页重复（服务端忽略 pageId）
        else:
            batch = [_c(4, "不该到达", 1)]
        return {"code": 0, "msg": "success", "data": {"count": len(batch), "comments": batch}}

    calls = _patch({
        "baseInfo": {"albumId": 38416783, "title": "【遁入不朽】一"},
        "albumComment/list": _comments,
    })
    try:
        out = ximalaya.fetch(TRACK_URL, 40)   # limit 40 → 最多请求 2 页
        assert out["title"] == "【遁入不朽】一"
        texts = [c["text"] for c in out["comments"]]
        assert texts == ["高赞", "中赞"], texts   # 空文本过滤 + commentId 去重
        assert out["comments"][0]["likes"] == 68
        assert out["comments"][0]["time"] == 1779245119    # 毫秒 → 秒
        assert out["comments"][1]["time"] == 1700000000
        urls = [u for u, _ in calls]
        assert any("baseInfo" in u for u in urls)
        n_list = sum(1 for u in urls if "albumComment/list" in u)
        assert n_list == 2, f"第 2 页整页重复应立即停止（共 2 次），实际请求评论 {n_list} 次"
        first = next(p for u, p in calls if "albumComment/list" in u)
        assert first.get("order") == 1 and first.get("albumId") == 38416783
    finally:
        _restore()
    print("[fetch.sound] baseInfo→album→list / 去重 / 毫秒转秒 / 重复即停 ok")


def test_single_page_when_limit_small():
    """limit≤20（top_comments 默认 20）只请求 1 页——多数场景无谓翻页不发生。"""
    calls = _patch({
        "baseInfo": {"albumId": 1, "title": "t"},
        "albumComment/list": {"code": 0, "data": {"count": 1, "comments": [_c(1, "评", 2)]}},
    })
    try:
        out = ximalaya.fetch(TRACK_URL, 20)
        assert [c["text"] for c in out["comments"]] == ["评"]
        n_list = sum(1 for u, _ in calls if "albumComment/list" in u)
        assert n_list == 1, f"limit=20 应只请求 1 页，实际 {n_list} 次"
    finally:
        _restore()
    print("[fetch.limit] ≤20 单页 ok")

def test_fetch_album_direct():
    """album 链接：跳过 baseInfo，直接用 albumId 请求评论接口。"""
    calls = _patch({"albumComment/list": {"code": 0, "data": {"comments": [_c(9, "好评", 7)]}}})
    try:
        out = ximalaya.fetch(ALBUM_URL, 5)
        assert [c["text"] for c in out["comments"]] == ["好评"]
        assert not any("baseInfo" in u for u, _ in calls), "album 链接不应请求 baseInfo"
        assert calls[0][1]["albumId"] == 38416783
    finally:
        _restore()
    print("[fetch.album] skip baseInfo ok")


def test_error_classification():
    """code!=0 → CommentsError（如实上报）；code=0 空评论 → 空列表交门面判 NotSupported。"""
    raised = None
    _patch({"albumComment/list": {"code": 500, "msg": "服务异常", "data": None}})
    try:
        ximalaya.fetch(ALBUM_URL, 10)
    except CommentsNotSupportedError:
        raised = "not_supported"
    except CommentsError:
        raised = "error"
    finally:
        _restore()
    assert raised == "error", "code!=0 应抛 CommentsError，不得伪装 NotSupported"

    _patch({"albumComment/list": {"code": 0, "msg": "success", "data": {"count": 0, "comments": []}}})
    try:
        assert ximalaya.fetch(ALBUM_URL, 10)["comments"] == []
    finally:
        _restore()
    print("[errors] code!=0 CommentsError / code=0 空列表 ok")


def test_facade_not_supported():
    """门面集成：真无评论时 fetch_comments 抛 CommentsNotSupportedError。"""
    _patch({"albumComment/list": {"code": 0, "data": {"count": 0, "comments": []}}})
    try:
        raised = False
        try:
            comments.fetch_comments(f"https://www.ximalaya.com/album/999001", refresh=True)
        except CommentsNotSupportedError:
            raised = True
        assert raised, "空评论应经门面抛 CommentsNotSupportedError"
    finally:
        _restore()
    print("[facade] not-supported ok")


if __name__ == "__main__":
    test_extract_ids()
    test_routing()
    test_fetch_sound_flow()
    test_single_page_when_limit_small()
    test_fetch_album_direct()
    test_error_classification()
    test_facade_not_supported()
    print("XIMALAYA COMMENTS TEST PASSED")
