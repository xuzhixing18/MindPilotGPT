"""QQ音乐解析器离线测试（含 i.y.qq.com 老式播放页形态）。

覆盖 backend/downloader/{qqmusic,common,generic} 与 backend/transcribe/subtitles：
- qqmusic.can_handle 域名判定（含 i.y.qq.com）；
- _normalize：壳页/列表页中文报错、ryqq_v2 路由改写、老式链接改写、分享短链重定向（打桩 urlopen）；
- i.y.qq.com：playsong.html 按 songmid/songid 参数换经典详情页（打桩 urlopen）、
  无参数跟随重定向、无关路径中文报错；
- 纯数字 songid → 老接口 fcg JSON 换 mid，失败回退 v2 页面 __INITIAL_DATA__（打桩 urlopen）；
- _patch_mv_formats：QQMusicVideo 缺字段修正与非 MV 提取器不动；
- _best_mv_format_id：按（档位, 体积）选最高档（打桩 generic.extract_raw）；
- common.apply_cookies：COOKIE_FILE / COOKIES_FROM_BROWSER 注入；
- subtitles._parse_lrc_file：LRC 歌词 → 字幕分段；
- backend.downloader.normalize 门面：命中 qqmusic 归一化，其余原样放行。

运行：D:\\ai-code\\MindPilot-GPT\\.venv\\Scripts\\python.exe tests\\test_qqmusic.py
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 必须先于 backend 导入设置临时库（backend.transcribe 导入链会触达 storage.db）
_TMP = tempfile.mkdtemp(prefix="qqmusic_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

from backend import storage  # noqa: E402
from backend.downloader import common, qqmusic  # noqa: E402
from backend.transcribe import subtitles  # noqa: E402

USER_PLAYSONG = "https://i.y.qq.com/v8/playsong.html?songid=736284469#webchat_redirect"
FREE_MID = "004Ti8rT003TaZ"


def test_can_handle():
    for url in (
        "https://y.qq.com/n/ryqq/songDetail/004Ti8rT003TaZ",
        "https://c.y.qq.com/base/fcgi-bin/u?__=abcd",
        "https://c6.y.qq.com/base/fcgi-bin/u?__=abcd",
        "https://m.y.qq.com/base/fcgi-bin/u?__=abcd",
        "https://i.y.qq.com/v8/playsong.html?songid=736284469",
        "https://i2.y.qq.com/n3/other/pages/playsong/index.html?songid=107762070",
    ):
        assert qqmusic.can_handle(url), url
    for url in (
        "https://www.douyin.com/video/7380123456789012345",
        "https://y.qq.com.evil.com/n/ryqq/songDetail/x",
        "https://example.com/n/ryqq/songDetail/x",
        "",
        "not-a-url",
    ):
        assert not qqmusic.can_handle(url), url
    print("[qqmusic] can_handle ok")


def test_normalize_errors_and_rewrite():
    for url in ("https://y.qq.com/n/ryqq_v2/player", "https://y.qq.com/n/ryqq/player/",
                "https://y.qq.com/portal/player.html"):
        try:
            qqmusic._normalize(url)
            raise AssertionError("shell page must raise")
        except ValueError as exc:
            assert "壳页" in str(exc), exc
    for path in ("albumDetail/000gXCTb2AhRR1", "playlist/1374105607", "singer/001BLpXF2DyJe2", "toplist/123"):
        for prefix in ("ryqq", "ryqq_v2"):
            try:
                qqmusic._normalize(f"https://y.qq.com/n/{prefix}/{path}")
                raise AssertionError("list page must raise")
            except ValueError as exc:
                assert "暂不支持" in str(exc), exc
    song = f"https://y.qq.com/n/ryqq/songDetail/{FREE_MID}"
    assert qqmusic._normalize(song) == song  # 详情页原样放行
    assert qqmusic._normalize("https://y.qq.com/n/yqq/mv/002Vsarh3SVU8K.html") == "https://y.qq.com/n/ryqq/mv/002Vsarh3SVU8K"
    assert qqmusic._normalize(f"https://y.qq.com/n/yqq/song/{FREE_MID}.html?ADTAG=x") == song
    # ryqq_v2 新路由改写为经典 ryqq（丢弃 query/fragment）
    v2_song = f"https://y.qq.com/n/ryqq_v2/songDetail/{FREE_MID}?ADTAG=h5_play_song&mnst=1.16#webchat_redirect"
    assert qqmusic._normalize(v2_song) == song
    assert qqmusic._normalize("https://y.qq.com/n/ryqq_v2/mv/002Vsarh3SVU8K?x=1") == "https://y.qq.com/n/ryqq/mv/002Vsarh3SVU8K"
    print("[qqmusic] normalize errors/rewrite ok")


class _FakeRespBody:
    def __init__(self, body: str):
        self._body = body

    def read(self):
        return self._body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeResp:
    def __init__(self, final: str):
        self._final = final

    def geturl(self):
        return self._final

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


_FCG_OK = '{"code":0,"data":[{"mid":"003UQwPW3OPxBy","name":"过了"}]}'
_FCG_EMPTY = '{"code":0,"data":[]}'
_PAGE_OK = (
    "<html><script>window.__INITIAL_DATA__ = "
    '{"notFound":false,"detail":{"id":736284469,"name":"过了"},'
    '"songList":[{"mid":"003UQwPW3OPxBy","id":736284469}]};'
    "</script></html>"
)
_PAGE_GONE = '<script>window.__INITIAL_DATA__ = {"notFound":true,"songList":[]};</script>'

_FCU_VIP = json.dumps({"info": {"code": 0, "data": {"track_info": {
    "mid": "001E7tqn0Z9Ng9", "title": "农民与土地 (Live)",
    "pay": {"pay_play": 1}, "file": {"media_mid": "001E7tqn0Z9Ng9"}}}}})
_FCU_VKEY_EMPTY = json.dumps({"req_1": {"code": 0, "data": {"midurlinfo": [
    {"purl": "", "filename": "M500x.mp3"}, {"purl": "", "filename": "C400x.m4a"}]}}})
_FCU_VKEY_TRIAL = json.dumps({"req_1": {"code": 0, "data": {"midurlinfo": [
    {"purl": "M500xxx.mp3", "filename": "M500x.mp3"}]}}})
_FCU_FREE = json.dumps({"info": {"code": 0, "data": {"track_info": {
    "mid": "004Ti8rT003TaZ", "title": "永夜のパレード",
    "pay": {"pay_play": 0}, "file": {"media_mid": "004Ti8rT003TaZ"}}}}})
_FCU_MV_GONE = json.dumps({"mvInfo": {"code": 0, "data": {
    "001E7tqn0Z9Ng9": {"code": 10002, "play_forbid_reason": None}}}})
_FCU_MV_OK = json.dumps({"mvInfo": {"code": 0, "data": {
    "002Vsarh3SVU8K": {"code": 0, "play_forbid_reason": None}}}})


def test_playsong_params():
    """i.y.qq.com 播放页：songmid 直换、songid 经老接口换、无参数跟随重定向。"""
    original = qqmusic.urllib.request.urlopen
    calls = []

    def fake(req, **kw):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        calls.append(url)
        if "fcg_play_single_song" in url:
            return _FakeRespBody(_FCG_OK)
        if "playsong" in url:
            return _FakeResp(f"https://y.qq.com/n/ryqq_v2/songDetail/{FREE_MID}?ADTAG=x")
        raise OSError(f"unexpected url {url}")

    qqmusic.urllib.request.urlopen = fake
    try:
        # songmid 参数：零网络调用直接构造经典 URL（主机统一为 y.qq.com）
        assert qqmusic._normalize("https://i.y.qq.com/v8/playsong.html?songmid=003UQwPW3OPxBy") \
            == "https://y.qq.com/n/ryqq/songDetail/003UQwPW3OPxBy"
        assert not calls
        # songid 参数：走老接口 fcg 换 mid
        assert qqmusic._normalize(USER_PLAYSONG) == "https://y.qq.com/n/ryqq/songDetail/003UQwPW3OPxBy"
        assert any("fcg_play_single_song" in c and "736284469" in c for c in calls)
        # 其他 i.y.qq.com 页面带 songid 参数同样可换
        assert qqmusic._normalize("https://i.y.qq.com/n2/m/share/details/song.html?songid=736284469&ADTAG=x") \
            == "https://y.qq.com/n/ryqq/songDetail/003UQwPW3OPxBy"
        # i2.y.qq.com 移动播放页变体（n3 playsong 路由）同样按 songid 换 mid
        assert qqmusic._normalize("https://i2.y.qq.com/n3/other/pages/playsong/index.html?songid=736284469&ADTAG=x") \
            == "https://y.qq.com/n/ryqq/songDetail/003UQwPW3OPxBy"
        # 无参数 playsong：跟随重定向 → 落地 v2 详情页再归一化
        assert qqmusic._normalize("https://i.y.qq.com/v8/playsong.html") \
            == f"https://y.qq.com/n/ryqq/songDetail/{FREE_MID}"
        # 无关 i.y.qq.com 路径：中文报错
        try:
            qqmusic._normalize("https://i.y.qq.com/n2/m/api/vip/task_info")
            raise AssertionError("param-less i.y.qq.com page must raise")
        except ValueError as exc:
            assert "无法解析该QQ音乐链接" in str(exc), exc
    finally:
        qqmusic.urllib.request.urlopen = original
    print("[qqmusic] playsong params ok")


def test_numeric_song_mid():
    """数字 songid → mid：老接口优先，失败回退 v2 页面；均失败报下架。"""
    num = "https://y.qq.com/n/ryqq/songDetail/736284469"
    original = qqmusic.urllib.request.urlopen
    seen = []

    def fake(req, **kw):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        seen.append(url)
        if "fcg_play_single_song" in url:
            return _FakeRespBody(_FCG_OK)
        raise OSError(f"unexpected url {url}")

    qqmusic.urllib.request.urlopen = fake
    try:
        assert qqmusic._normalize(num) == "https://y.qq.com/n/ryqq/songDetail/003UQwPW3OPxBy"
        assert len(seen) == 1 and "fcg_play_single_song" in seen[0]

        # 老接口空数据 → 回退页面 __INITIAL_DATA__
        seen.clear()

        def fake_fallback(req, **kw):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            seen.append(url)
            if "fcg_play_single_song" in url:
                return _FakeRespBody(_FCG_EMPTY)
            if "ryqq_v2/songDetail" in url:
                return _FakeRespBody(_PAGE_OK)
            raise OSError(f"unexpected url {url}")

        qqmusic.urllib.request.urlopen = fake_fallback
        assert qqmusic._normalize(num) == "https://y.qq.com/n/ryqq/songDetail/003UQwPW3OPxBy"
        assert any("ryqq_v2/songDetail" in c for c in seen)

        # 老接口空 + 页面 notFound → 下架提示
        def fake_gone(req, **kw):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "fcg_play_single_song" in url:
                return _FakeRespBody(_FCG_EMPTY)
            if "ryqq_v2/songDetail" in url:
                return _FakeRespBody(_PAGE_GONE)
            raise OSError(f"unexpected url {url}")

        qqmusic.urllib.request.urlopen = fake_gone
        try:
            qqmusic._normalize(num)
            raise AssertionError("gone song must raise")
        except ValueError as exc:
            assert "下架" in str(exc), exc
    finally:
        qqmusic.urllib.request.urlopen = original
    print("[qqmusic] numeric songid -> mid ok")


def test_share_redirect():
    song_final = f"https://y.qq.com/n/ryqq/songDetail/{FREE_MID}"
    share = "https://c.y.qq.com/base/fcgi-bin/u?__=abcd"
    original = qqmusic.urllib.request.urlopen
    try:
        qqmusic.urllib.request.urlopen = lambda req, **kw: _FakeResp(song_final)
        assert qqmusic._normalize(share) == song_final

        # 子域名变体 c6.y.qq.com（真实分享短链主机）同样跟随重定向
        share6 = "https://c6.y.qq.com/base/fcgi-bin/u?__=RYsU4B"
        calls = []

        def fake_record(req, **kw):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            calls.append(url)
            if "fcg_play_single_song" in url:
                return _FakeRespBody(_FCG_OK)
            return _FakeResp(f"https://y.qq.com/n/ryqq_v2/songDetail/107762070?ADTAG=h5_play_song")

        qqmusic.urllib.request.urlopen = fake_record
        assert qqmusic._normalize(share6) == "https://y.qq.com/n/ryqq/songDetail/003UQwPW3OPxBy"
        assert calls[0] == share6
        # 落地页是 v2 数字 songid：需走老接口换 mid（fcg 调用随归一化发生）
        assert any("fcg_play_single_song" in c and "107762070" in c for c in calls)

        qqmusic.urllib.request.urlopen = lambda req, **kw: _FakeResp("https://y.qq.com/n/ryqq_v2/player")
        try:
            qqmusic._normalize(share)
            raise AssertionError("redirect to shell must raise")
        except ValueError as exc:
            assert "壳页" in str(exc)

        qqmusic.urllib.request.urlopen = lambda req, **kw: _FakeResp("https://example.com/some")
        try:
            qqmusic._normalize(share)
            raise AssertionError("redirect to foreign host must raise")
        except ValueError as exc:
            assert "短链" in str(exc)

        def fake_fail(req, **kw):
            raise OSError("boom")

        qqmusic.urllib.request.urlopen = fake_fail
        try:
            qqmusic._normalize(share)
            raise AssertionError("urlopen failure must raise")
        except ValueError as exc:
            assert "分享链接" in str(exc)
    finally:
        qqmusic.urllib.request.urlopen = original
    print("[qqmusic] share redirect ok")


def test_facade_normalize():
    """backend.downloader.normalize 门面：命中 qqmusic 则归一化，其余原样返回。"""
    from backend import downloader

    original = qqmusic._check_paywall
    qqmusic._check_paywall = lambda url: None  # 离线测试：预检打桩，不碰真实网络
    try:
        v2 = f"https://y.qq.com/n/ryqq_v2/songDetail/{FREE_MID}?x=1#frag"
        assert downloader.normalize(v2) == f"https://y.qq.com/n/ryqq/songDetail/{FREE_MID}"
        douyin = "https://www.douyin.com/video/7380123456789012345"
        assert downloader.normalize(douyin) == douyin  # 无归一化的平台原样放行
        try:
            downloader.normalize("https://y.qq.com/n/ryqq_v2/player")
            raise AssertionError("shell page must raise")
        except ValueError as exc:
            assert "壳页" in str(exc), exc
    finally:
        qqmusic._check_paywall = original
    print("[qqmusic] facade normalize ok")


def test_paywall_check():
    """VIP 歌曲/失效 MV 预检：中文指引；免费/试听流/异常/已配置登录态时放行。"""
    saved = {k: os.environ.get(k) for k in ("COOKIE_FILE", "COOKIES_FROM_BROWSER")}
    original = qqmusic.urllib.request.urlopen
    vip_url = "https://y.qq.com/n/ryqq/songDetail/001E7tqn0Z9Ng9"

    def body_of(req) -> str:
        return (getattr(req, "data", None) or b"").decode("utf-8", errors="ignore")

    try:
        for key in ("COOKIE_FILE", "COOKIES_FROM_BROWSER"):
            os.environ.pop(key, None)

        # VIP 歌曲（匿名无播放流）→ 中文指引（含歌名）
        def vip_fake(req, **kw):
            body = body_of(req)
            if "get_song_detail" in body:
                return _FakeRespBody(_FCU_VIP)
            if "GetVkeyServer" in body:
                return _FakeRespBody(_FCU_VKEY_EMPTY)
            raise OSError(f"unexpected body {body[:80]}")

        qqmusic.urllib.request.urlopen = vip_fake
        try:
            qqmusic.normalize(vip_url)
            raise AssertionError("VIP song must raise")
        except ValueError as exc:
            assert "农民与土地" in str(exc) and "VIP" in str(exc), exc

        # 匿名仍有试听流 → 不拦截（与 yt-dlp 行为一致）
        def trial_fake(req, **kw):
            body = body_of(req)
            if "get_song_detail" in body:
                return _FakeRespBody(_FCU_VIP)
            if "GetVkeyServer" in body:
                return _FakeRespBody(_FCU_VKEY_TRIAL)
            raise OSError(f"unexpected body {body[:80]}")

        qqmusic.urllib.request.urlopen = trial_fake
        assert qqmusic.normalize(vip_url) == vip_url

        # 免费歌曲 → 原样放行（只发一次详情查询，不查 vkey）
        fcu_calls = []

        def free_fake(req, **kw):
            body = body_of(req)
            fcu_calls.append(body)
            if "get_song_detail" in body:
                return _FakeRespBody(_FCU_FREE)
            raise OSError(f"unexpected body {body[:80]}")

        qqmusic.urllib.request.urlopen = free_fake
        free_url = f"https://y.qq.com/n/ryqq/songDetail/{FREE_MID}"
        assert qqmusic.normalize(free_url) == free_url
        assert len(fcu_calls) == 1 and "GetVkeyServer" not in fcu_calls[0]

        # 失效 MV → 中文指引；有效 MV → 原样放行
        def mv_gone_fake(req, **kw):
            return _FakeRespBody(_FCU_MV_GONE)

        qqmusic.urllib.request.urlopen = mv_gone_fake
        try:
            qqmusic.normalize("https://y.qq.com/n/ryqq/mv/001E7tqn0Z9Ng9")
            raise AssertionError("gone MV must raise")
        except ValueError as exc:
            assert "不存在或已下架" in str(exc), exc
        qqmusic.urllib.request.urlopen = lambda req, **kw: _FakeRespBody(_FCU_MV_OK)
        mv_url = "https://y.qq.com/n/ryqq/mv/002Vsarh3SVU8K"
        assert qqmusic.normalize(mv_url) == mv_url

        # 接口异常 → 放行（尽力而为）
        def fail_fake(req, **kw):
            raise OSError("network down")

        qqmusic.urllib.request.urlopen = fail_fake
        assert qqmusic.normalize(vip_url) == vip_url

        # 已配置登录态 → 不拦截（零网络调用）
        calls = []

        def counting_fake(req, **kw):
            calls.append(req.full_url if hasattr(req, "full_url") else str(req))
            raise OSError("should not be called")

        cookie_path = Path(_TMP) / "cookies.txt"
        cookie_path.write_text("# Netscape HTTP Cookie File", encoding="utf-8")
        os.environ["COOKIE_FILE"] = str(cookie_path)
        qqmusic.urllib.request.urlopen = counting_fake
        assert qqmusic.normalize(vip_url) == vip_url
        assert not calls
        os.environ.pop("COOKIE_FILE", None)
        os.environ["COOKIES_FROM_BROWSER"] = "chrome"
        assert qqmusic.normalize(vip_url) == vip_url
        assert not calls
    finally:
        qqmusic.urllib.request.urlopen = original
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print("[qqmusic] paywall check ok")


def test_vip_error_mapping():
    """解析阶段兜底：yt-dlp 的英文错误翻译为中文指引（预检失效时仍可读）。"""
    from backend.downloader import generic

    original_check = qqmusic._check_paywall
    original_info = generic.extract_info
    qqmusic._check_paywall = lambda url: None
    try:
        def raising(url):
            raise ValueError(
                "无法解析该链接：ERROR: [qqmusic] 001E7tqn0Z9Ng9: This video is only "
                "available for registered users. Use --cookies-from-browser or "
                "--cookies for the authentication.")

        generic.extract_info = raising
        try:
            qqmusic.extract_info("https://y.qq.com/n/ryqq/songDetail/001E7tqn0Z9Ng9")
            raise AssertionError("must raise")
        except ValueError as exc:
            assert "VIP" in str(exc) and "registered users" not in str(exc), exc
    finally:
        qqmusic._check_paywall = original_check
        generic.extract_info = original_info
    print("[qqmusic] vip error mapping ok")


def test_patch_mv_formats():
    info = {
        "extractor_key": "QQMusicVideo",
        "formats": [
            # yt-dlp 后处理会给缺失字段显式填 None（不能用 setdefault 修正）
            {"format_id": "9814", "url": "https://mv6.music.tc.qq.com/a.mp4", "filesize": 100,
             "vcodec": None, "acodec": None, "resolution": None, "height": None},
            {"format_id": "9834", "url": "https://mv6.music.tc.qq.com/b.mp4", "filesize": 300,
             "vcodec": None, "acodec": None, "resolution": None, "height": None},
            {"format_id": "9824", "url": "https://mv6.music.tc.qq.com/c.mp4", "filesize": 200,
             "vcodec": None, "acodec": None, "resolution": None, "height": None},
        ],
    }
    qqmusic._patch_mv_formats(info)
    labels = [f["resolution"] for f in info["formats"]]
    assert labels == ["标清", "超清", "高清"], labels
    assert all(f["vcodec"] == "avc1" and f["acodec"] == "aac" for f in info["formats"])
    assert [f["preference"] for f in info["formats"]] == [1, 3, 2]

    other = {"extractor_key": "BiliBili", "formats": [{"format_id": "1", "vcodec": None}]}
    qqmusic._patch_mv_formats(other)
    assert other["formats"][0]["vcodec"] is None  # 非 MV 提取器不动
    print("[qqmusic] patch mv formats ok")


def test_best_mv_format_id():
    from backend.downloader import generic

    original = generic.extract_raw
    generic.extract_raw = lambda url: {
        "extractor_key": "QQMusicVideo",
        "formats": [
            {"format_id": "9814", "filesize": 100},
            {"format_id": "9834", "filesize": 300},
            {"format_id": "9824", "filesize": 200},
        ],
    }
    try:
        assert qqmusic._best_mv_format_id("https://y.qq.com/n/ryqq/mv/x") == "9834"
    finally:
        generic.extract_raw = original
    print("[qqmusic] best mv format ok")


def test_lrc_parse():
    content = (
        "[ti:过了]\n"
        "[ar:郁可唯]\n"
        "[00:00.00]词：某人\n"
        "[00:05.20]第一句歌词\n"
        "[00:08.00][00:12.50]重复的副歌\n"
        "[00:20.00]最后一句\n"
    )
    path = Path(_TMP) / "test.lrc"
    path.write_text(content, encoding="utf-8")
    segments = subtitles._parse_lrc_file(path)
    texts = [s["text"] for s in segments]
    # 元数据行（ti/ar）丢弃；时间戳行保留；不同时间的重复副歌不去重
    assert texts == ["词：某人", "第一句歌词", "重复的副歌", "重复的副歌", "最后一句"], texts
    starts = [s["start"] for s in segments]
    assert starts == sorted(starts)
    assert segments[-1]["end"] == round(segments[-1]["start"] + 4.0, 2)  # 末行默认 4 秒
    print("[qqmusic] lrc parse ok")


def test_apply_cookies():
    saved = {k: os.environ.get(k) for k in ("COOKIE_FILE", "COOKIES_FROM_BROWSER")}
    try:
        os.environ.pop("COOKIE_FILE", None)
        os.environ.pop("COOKIES_FROM_BROWSER", None)
        opts: dict = {}
        common.apply_cookies(opts)
        assert not opts  # 未配置不改动

        os.environ["COOKIE_FILE"] = str(Path(_TMP) / "cookies.txt")
        Path(os.environ["COOKIE_FILE"]).write_text("# Netscape HTTP Cookie File", encoding="utf-8")
        common.apply_cookies(opts)
        assert opts["cookiefile"] == os.environ["COOKIE_FILE"]
        opts.pop("cookiefile")

        # 文件不存在 + 浏览器配置 → cookiesfrombrowser 四元组
        os.environ["COOKIE_FILE"] = str(Path(_TMP) / "missing.txt")
        os.environ["COOKIES_FROM_BROWSER"] = "chrome:Profile 1"
        common.apply_cookies(opts)
        assert opts["cookiesfrombrowser"] == ("chrome", "Profile 1", None, None)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print("[qqmusic] apply_cookies ok")


def _cleanup():
    try:
        storage.db.dispose()
    except AttributeError:
        pass
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    test_can_handle()
    test_normalize_errors_and_rewrite()
    test_playsong_params()
    test_numeric_song_mid()
    test_share_redirect()
    test_facade_normalize()
    test_paywall_check()
    test_vip_error_mapping()
    test_patch_mv_formats()
    test_best_mv_format_id()
    test_lrc_parse()
    test_apply_cookies()
    _cleanup()
    print("QQMUSIC TEST PASSED")
