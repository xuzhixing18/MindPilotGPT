"""抖音专用解析模块测试。

包含：
- 离线：链接识别 is_douyin_url、分享文案取链 extract_first_url、playwm→play 逻辑
- 在线探针：公开 iteminfo API 是否可达（返回 JSON 即视为连通）

运行：python tests/test_douyin.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.downloader import douyin  # noqa: E402


def test_detection():
    assert douyin.can_handle("https://v.douyin.com/abc123/")  # 新调度契约
    assert douyin.is_douyin_url("https://v.douyin.com/abc123/")  # 旧名（别名）仍可用
    assert douyin.is_douyin_url("https://www.douyin.com/video/7123456789012345678")
    assert douyin.is_douyin_url("https://www.iesdouyin.com/share/video/7123/")
    assert not douyin.is_douyin_url("https://www.bilibili.com/video/BV1GJ411x7h7")
    assert not douyin.is_douyin_url("https://youtube.com/watch?v=x")
    print("[detection] ok")


def test_extract_first_url():
    text = "7.28 复制打开抖音，看看作品 https://v.douyin.com/abc123/ 复制此链接"
    assert douyin.extract_first_url(text) == "https://v.douyin.com/abc123/"
    assert douyin.extract_first_url("https://v.douyin.com/x/") == "https://v.douyin.com/x/"
    print("[extract_first_url] ok")


def test_playwm_replace():
    item = {"video": {"play_addr": {"url_list": ["https://aweme.snssdk.com/aweme/v1/playwm/?x=1"]}}}
    assert douyin.no_watermark_play_url(item) == "https://aweme.snssdk.com/aweme/v1/play/?x=1"
    print("[playwm->play] ok")


def test_api_reachable():
    """在线探针：公开 iteminfo API 可达性（dummy id 允许返回空列表）。"""
    try:
        data = douyin._session.get(
            douyin.API_URL, params={"item_ids": "0"}, timeout=douyin._TIMEOUT
        ).json()
        print("[api-probe] reachable, keys:", sorted(data.keys())[:6])
    except Exception as exc:  # noqa: BLE001
        print("[api-probe] NOT reachable:", exc)


if __name__ == "__main__":
    test_detection()
    test_extract_first_url()
    test_playwm_replace()
    test_api_reachable()
    print("DOUYIN TEST DONE")
