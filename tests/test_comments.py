"""高赞评论 MVP（无大模型）离线测试：不触网、不需真实 Key。

覆盖：
- top_comments：过滤空文本 → 按点赞降序 → 截断 limit
- repo        ：put/get_comments 往返一致；TTL（COMMENTS_CACHE_HOURS）过期视为未命中，<=0 永不过期
- 门面         ：monkeypatch 假平台模块后，fetch_comments 首次计算并落库、再命中缓存（cached=True
                且不再调用底层）、refresh=True 强制重算；空评论抛 CommentsNotSupportedError
- 抖音         ：响应分类——签名网关(5/-99999/非JSON)/真无评论 → NotSupported；正常 → 归一化

运行：python tests/test_comments.py
"""
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# db.py 导入即按 DATABASE_URL 建 engine，故须在导入 backend.* 前指向临时库
_TMP_DIR = Path(tempfile.mkdtemp(prefix="mp_comments_test_"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP_DIR / 'test_comments.db').as_posix()}"
os.environ["CACHE_ENABLED"] = "true"
os.environ.pop("COMMENTS_CACHE_HOURS", None)

from backend import comments, storage  # noqa: E402
from backend.storage import keys, models, repo  # noqa: E402

storage.init_db()


def _make_fake(raw_comments):
    """构造一个与平台模块同契约（can_handle/fetch）的假模块，__name__ 决定 source 标签。"""
    mod = types.ModuleType("fakeplat")
    calls = {"n": 0}

    def _fetch(url, limit):
        calls["n"] += 1
        return {"title": "假标题", "comments": list(raw_comments)}

    mod.can_handle = lambda url: True
    mod.fetch = _fetch
    return mod, calls


def test_top_comments():
    raw = [
        {"author": "a", "text": "低赞", "likes": 1, "time": 1},
        {"author": "b", "text": "高赞", "likes": 999, "time": 2},
        {"author": "c", "text": "   ", "likes": 100000, "time": 3},   # 空文本应被过滤
        {"author": "d", "text": "中赞", "likes": 50, "time": 4},
    ]
    top = comments.top_comments(raw, limit=2)
    assert len(top) == 2
    assert [c["text"] for c in top] == ["高赞", "中赞"]      # 降序 + 过滤空 + 截断
    assert top[0]["likes"] == 999
    assert comments.top_comments([], 5) == []
    print("[top_comments] sort/filter/limit ok")


def test_repo_roundtrip():
    url = "https://www.bilibili.com/video/BV1comments"
    key = keys.comments_key(url)
    result = {
        "title": "标题", "source": "bilibili", "total": 1,
        "comments": [{"author": "x", "text": "好", "likes": 7, "time": 123}],
    }
    repo.put_comments(key, result, url, keys.normalize_url(url))
    got = repo.get_comments(key)
    assert got is not None
    assert got["title"] == "标题" and got["source"] == "bilibili" and got["total"] == 1
    assert got["comments"] == [{"author": "x", "text": "好", "likes": 7, "time": 123}]
    assert "cached" not in got
    print("[repo] comments roundtrip ok")


def test_repo_ttl():
    url = "https://youtube.com/watch?v=commentsttl"
    key = keys.comments_key(url)
    repo.put_comments(key, {"title": "t", "source": "generic", "total": 1,
                            "comments": [{"text": "hi", "likes": 1}]}, url, keys.normalize_url(url))
    assert repo.get_comments(key) is not None           # 新鲜命中

    with storage.session() as s:                        # 拨到远超默认 12h 之前
        s.get(models.Comment, key).created_at = datetime.now(timezone.utc) - timedelta(hours=999)
    assert repo.get_comments(key) is None               # 过期未命中

    os.environ["COMMENTS_CACHE_HOURS"] = "0"            # <=0 永不过期
    try:
        assert repo.get_comments(key) is not None
    finally:
        os.environ.pop("COMMENTS_CACHE_HOURS", None)
    print("[repo] comments TTL ok")


def test_facade_cache():
    raw = [
        {"author": "lo", "text": "低", "likes": 3, "time": 1},
        {"author": "hi", "text": "高", "likes": 900, "time": 2},
    ]
    mod, calls = _make_fake(raw)
    old = comments._PLATFORMS
    comments._PLATFORMS = [mod]
    try:
        url = "https://fake.example/v/facade1"
        first = comments.fetch_comments(url)
        assert first["cached"] is False
        assert first["source"] == "fakeplat"
        assert first["total"] == 2
        assert [c["text"] for c in first["comments"]] == ["高", "低"]   # 服务端已排序
        assert calls["n"] == 1

        second = comments.fetch_comments(url)            # 命中缓存，不再抓取
        assert second["cached"] is True
        assert calls["n"] == 1

        third = comments.fetch_comments(url, refresh=True)  # 强制重算
        assert third["cached"] is False
        assert calls["n"] == 2
    finally:
        comments._PLATFORMS = old
    print("[facade] cache hit / refresh ok")


def test_not_supported():
    mod, _calls = _make_fake([])                          # 空评论
    old = comments._PLATFORMS
    comments._PLATFORMS = [mod]
    try:
        raised = False
        try:
            comments.fetch_comments("https://fake.example/v/empty1")
        except comments.CommentsNotSupportedError:
            raised = True
        assert raised, "空评论应抛 CommentsNotSupportedError"
    finally:
        comments._PLATFORMS = old
    print("[facade] not-supported ok")


def test_routing():
    """_pick 路由：B 站/抖音命中专用，其余回退 generic。"""
    assert comments._pick("https://www.bilibili.com/video/BV1xx").__name__.endswith("bilibili")
    assert comments._pick("https://v.douyin.com/abc/").__name__.endswith("douyin")
    assert comments._pick("https://www.youtube.com/watch?v=abc").__name__.endswith("generic")
    print("[routing] _pick ok")


class _FakeResp:
    """模拟 requests 响应：raise_for_status 空操作，json() 可配置抛错。"""

    def __init__(self, payload=None, nonjson=False):
        self._payload = payload
        self._nonjson = nonjson

    def raise_for_status(self):
        pass

    def json(self):
        if self._nonjson:
            raise ValueError("no json")
        return self._payload


def test_douyin_classification():
    """抖音响应分类：签名网关/真无评论 → NotSupported；正常 → 归一化。"""
    from backend.comments import douyin as dy
    old_get = dy.requests.get
    old_resolve = dy._douyin.resolve_video_id
    old_first = dy._douyin.extract_first_url
    dy._douyin.resolve_video_id = lambda u: ("123", u)
    dy._douyin.extract_first_url = lambda u: u
    url = "https://www.douyin.com/video/123"
    try:
        for payload, nonjson in (
            ({"status_code": 5, "status_msg": "参数不合法"}, False),   # 签名网关
            ({"status_code": -99999}, False),                          # 需签名
            ({"status_code": 0, "comments": []}, False),              # 真无评论
            (None, True),                                             # 风控非 JSON
        ):
            dy.requests.get = lambda *a, _p=payload, _n=nonjson, **k: _FakeResp(_p, _n)
            raised = False
            try:
                dy.fetch(url, 10)
            except comments.CommentsNotSupportedError:
                raised = True
            assert raised, f"payload={payload} nonjson={nonjson} 应抛 NotSupported"

        dy.requests.get = lambda *a, **k: _FakeResp({"status_code": {"StatusCode": 0}, "comments": [
            {"text": "好", "digg_count": 5, "user": {"nickname": "n"}, "createTime": 1},
            {"text": "   ", "digg_count": 9},                          # 空文本过滤
        ]})
        out = dy.fetch(url, 10)
        assert out["comments"] == [{"author": "n", "text": "好", "likes": 5, "time": 1}]
    finally:
        dy.requests.get = old_get
        dy._douyin.resolve_video_id = old_resolve
        dy._douyin.extract_first_url = old_first
    print("[douyin] response classification ok")


if __name__ == "__main__":
    test_top_comments()
    test_repo_roundtrip()
    test_repo_ttl()
    test_facade_cache()
    test_not_supported()
    test_routing()
    test_douyin_classification()
    print("COMMENTS TEST PASSED")
