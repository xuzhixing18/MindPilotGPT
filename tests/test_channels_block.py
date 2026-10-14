"""微信视频号拦截器（downloader/wechat_channels.py）的离线测试（不触网、无 DB）。

覆盖：
- can_handle：命中分享短链与展开后的 finder-preview 页；不误伤公众号文章/其他平台
- 门面调度：视频号 URL 被 _pick 选中拦截器而非 generic（registry 接线正确）
- extract_info / download：抛 ValueError（/api/info 上层按 400 + detail 原样透出），
  文案含「视频号」中文指引且为生英文 Unsupported URL 不再出现在用户侧

运行：python tests/test_channels_block.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import downloader  # noqa: E402
from backend.downloader import registry, wechat_channels  # noqa: E402

# 用户实际报错的两个 URL：粘贴的短链 + yt-dlp 跟进 302 后的最终页
BUG_URLS = (
    "https://weixin.qq.com/sph/AnoMXazSbF",
    "https://channels.weixin.qq.com/finder-preview/pages/sph?id=AnoMXazSbF",
)


def test_can_handle_positive():
    for url in BUG_URLS:
        assert wechat_channels.can_handle(url), url
    # 大小写/前后空白/子域同样命中
    assert wechat_channels.can_handle("  https://WEIXIN.QQ.COM/sphAbc ") is True or \
        wechat_channels.can_handle("https://WEIXIN.QQ.COM/sph/Abc") is True
    assert wechat_channels.can_handle("https://channels.weixin.qq.com/") is True
    print("[block] can_handle positive ok")


def test_can_handle_negative():
    # 公众号文章（mp.weixin.qq.com）不在拦截范围，仍走通用解析
    assert wechat_channels.can_handle("https://mp.weixin.qq.com/s/TCy2heoxXROz") is False
    # weixin.qq.com 非 /sph 路径不误伤
    assert wechat_channels.can_handle("https://weixin.qq.com/") is False
    # 已支持平台与其他不受影响
    for url in ("https://www.douyin.com/video/123", "https://www.bilibili.com/video/BV1xx",
                "https://www.youtube.com/watch?v=abc", "", "not a url"):
        assert wechat_channels.can_handle(url) is False, url
    print("[block] can_handle negative ok")


def test_registry_wiring():
    """registry 首位接线，且门面 _pick 对报错 URL 选中拦截器（绝不进 yt-dlp）。"""
    assert registry.SPECIFIC_EXTRACTORS[0] is wechat_channels
    for url in BUG_URLS:
        assert downloader._pick(url) is wechat_channels, url
    print("[block] registry wiring ok")


def test_friendly_hint():
    """extract_info/download 抛 ValueError（400 链路与 douyin 同构），文案为中文指引。"""
    for url in BUG_URLS:
        for fn, kwargs in ((downloader.extract_info, {}), (downloader.download, {})):
            try:
                fn(url, **kwargs)
                raise AssertionError(f"{fn.__name__} 应当抛出")
            except ValueError as exc:
                msg = str(exc)
                assert "视频号" in msg and "微信" in msg, msg
                assert "Unsupported URL" not in msg   # 生英文错误不再到达用户
    print("[block] friendly hint ok")


if __name__ == "__main__":
    test_can_handle_positive()
    test_can_handle_negative()
    test_registry_wiring()
    test_friendly_hint()
    print("CHANNELS BLOCK TEST PASSED")
