"""解析下载器的共享基础设施。

generic（通用 / yt-dlp）与各平台专用解析器（douyin 等）共用的常量与工具集中在此，
避免重复实现：临时下载目录、ffmpeg 能力探测、文件名清洗。

本模块不依赖任何具体解析器，处于依赖链末端，可被安全导入（无循环依赖风险）。
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

# 下载临时目录：所有服务端下载文件先落到这里，回传完成后由接口清理
DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "mindpilot_downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def ffmpeg_path() -> str | None:
    """探测可用的 ffmpeg 可执行文件路径。

    优先级：系统 PATH 上的 ffmpeg → pip 包 imageio-ffmpeg 内置的二进制。
    后者让项目「零配置、无需管理员权限」即可合并音视频，保持轻量。
    """
    sys_ffmpeg = shutil.which("ffmpeg")
    if sys_ffmpeg:
        return sys_ffmpeg
    try:
        import imageio_ffmpeg

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled and Path(bundled).exists():
            return bundled
    except Exception:  # noqa: BLE001 imageio-ffmpeg 未安装或获取失败时忽略
        pass
    return None


def ffmpeg_available() -> bool:
    """是否具备 ffmpeg 能力（用于合并「纯视频流 + 纯音频流」）。

    B站 / YouTube 等平台高清视频普遍采用音视频分离（DASH），
    需要 ffmpeg 合并；具备后即可解锁完整清晰度选项。
    """
    return ffmpeg_path() is not None


def sanitize_filename(name: str, default: str = "video") -> str:
    """清洗文件名中的非法字符，保证跨平台安全（Windows/Unix 通用）。"""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name or "")
    cleaned = cleaned.strip().rstrip(".")[:120]  # 去首尾空白与结尾点，并限制长度
    return cleaned or default


def apply_cookies(opts: dict[str, Any]) -> None:
    """按需向 yt-dlp 参数注入登录态 Cookie（二选一，前者优先）。

    B站等平台的会员内容、QQ音乐 VIP 歌曲需登录态才能解析，无 Cookie 时
    保持匿名降级行为。支持两种来源：
    - COOKIE_FILE         ：cookies.txt（Netscape 格式）文件路径
    - COOKIES_FROM_BROWSER：从浏览器读取，如 chrome / edge / "chrome:Profile 1"
    未配置或不生效时不改动 opts。
    """
    cookie_file = (os.getenv("COOKIE_FILE") or "").strip()
    if cookie_file:
        path = Path(cookie_file).expanduser()
        if path.is_file():
            opts["cookiefile"] = str(path)
            return
    browser = (os.getenv("COOKIES_FROM_BROWSER") or "").strip()
    if browser:
        parts = [p.strip() for p in browser.split(":")]
        name = parts[0]
        profile = parts[1] if len(parts) > 1 and parts[1] else None
        # yt-dlp 约定的四元组：(browser, profile, keyring, container)
        opts["cookiesfrombrowser"] = (name, profile, None, None)


def cookies_configured() -> bool:
    """登录态 Cookie 是否已配置（与 apply_cookies 同一判据，供付费预检复用）。"""
    cookie_file = (os.getenv("COOKIE_FILE") or "").strip()
    if cookie_file and Path(cookie_file).expanduser().is_file():
        return True
    return bool((os.getenv("COOKIES_FROM_BROWSER") or "").strip())


# ---- yt-dlp 提取级临时网络故障重试 ----
# 网易云音乐等平台接口会偶发「Remote end closed connection without response」类
# 连接重置，重置窗口通常只有几秒；但 yt-dlp 的提取阶段
# （YoutubeDL._handle_extraction_exceptions）对网络错误不做任何重试
# （'retries' 参数只作用于文件下载阶段），一次失败即整体报错。
# 因此在各调用点（解析/字幕/音频兜底）外包一层带间隔的重试。

NETWORK_RETRY_ATTEMPTS = 5  # 总尝试次数（含首次），最后一次重试在第 ~15 秒
NETWORK_RETRY_DELAY = 1.5   # 重试间隔基数（秒），随重试次数递增

# 「重试有机会成功」的临时故障签名。平台明确的业务错误（Unsupported URL /
# 登录要求 / 无可用格式等）不在此列，避免空转重试掩盖真实问题。
_TRANSIENT_NETWORK_SIGNATURES = (
    "RemoteDisconnected",
    "Remote end closed connection without response",
    "Connection aborted",
    "Connection reset by peer",
    "timed out",
    "Read timed out",
    "HTTP Error 502",
    "HTTP Error 503",
    "HTTP Error 504",
)

TRANSIENT_NETWORK_HINT = "网络连接被服务端中断，已自动重试多次仍失败，请稍后重试。"


def is_transient_network_error(exc: BaseException) -> bool:
    """是否为临时性网络故障（连接被重置/中断/超时、网关 5xx）。"""
    msg = str(exc)
    return any(sig in msg for sig in _TRANSIENT_NETWORK_SIGNATURES)


def extract_with_retry(opts: dict[str, Any], url: str, *, download: bool):
    """yt-dlp extract_info + 临时网络故障重试（解析/字幕/音频各调用点共用）。

    临时故障按 NETWORK_RETRY_DELAY 递增间隔重试 NETWORK_RETRY_ATTEMPTS 次；
    非临时错误立即抛出；重试耗尽后抛出最后一次的 DownloadError，由调用点
    按既有文案包装（无法解析 / 无法获取字幕 / 下载音频失败）。
    """
    last_exc: DownloadError | None = None
    for attempt in range(NETWORK_RETRY_ATTEMPTS):
        try:
            with YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=download)
        except DownloadError as exc:
            if not is_transient_network_error(exc):
                raise
            last_exc = exc
            if attempt + 1 < NETWORK_RETRY_ATTEMPTS:
                time.sleep(NETWORK_RETRY_DELAY * (attempt + 1))
    raise last_exc  # type: ignore[misc]
