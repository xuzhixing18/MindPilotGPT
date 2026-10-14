"""微信视频号链接的显式拦截提示（注册表范式模块，但**不是真正的解析器**）。

为什么必须拦截而不是解析：视频号视频流地址仅在微信客户端登录态内下发，分享短链
（weixin.qq.com/sph/{id}）302 后的 finder-preview 页对匿名服务端只是"请在微信
打开"的二维码预览壳，不含任何视频地址。yt-dlp 无此平台提取器且官方拒绝添加需
登录态平台的 extractor——走 generic 只会白跑完整超时链，最后把生英文
``Unsupported URL`` 抛给用户。本模块在门面调度即拦截，返回明确中文指引。

技术留档（2026-10 调研）：社区可行方案均不可服务端化——PC 微信本地代理抓包
（web 服务器无微信客户端）、借用元宝/微信 Cookie（合规与稳定性雷区）、第三方
公共解析 Worker（可用性无保障且泄露用户链接）。若未来接入需另立决策。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

# 前端 toast 按纯文本展示，指引保持单行
HINT = (
    "视频号视频由微信加密、仅在微信客户端登录态内播放，服务端无法自动解析"
    "（非本站故障）。建议在电脑微信打开该视频用「下载视频」保存到本地；"
    "若同一内容在抖音、B站、小红书等平台也有发布，可粘贴对应平台链接解析。"
)


class ChannelsUnsupportedError(ValueError):
    """视频号不支持服务端解析（上层按 400 + 中文指引处理）。"""


def can_handle(url: str) -> bool:
    """统一调度契约：识别视频号分享短链与频道页域名（不发起任何网络请求）。"""
    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return False
    host = (parsed.netloc or "").lower()
    # 分享短链：weixin.qq.com/sph/{id}（公众号 mp.weixin.qq.com 不在此列，不误伤）
    if host in ("weixin.qq.com", "www.weixin.qq.com"):
        return parsed.path.lower().startswith("/sph")
    # 展开后的频道/预览页：channels.weixin.qq.com 及其子域全部路径
    return host == "channels.weixin.qq.com" or host.endswith(".channels.weixin.qq.com")


def extract_info(url: str) -> dict[str, Any]:
    """拦截：不尝试解析，直接给出中文指引。"""
    raise ChannelsUnsupportedError(HINT)


def download(url: str, format_id: str | None = None) -> dict[str, Any]:
    """拦截：与 extract_info 同语义（正常流程走不到这里，防御性补齐门面契约）。"""
    raise ChannelsUnsupportedError(HINT)
