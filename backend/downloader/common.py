"""解析下载器的共享基础设施。

generic（通用 / yt-dlp）与各平台专用解析器（douyin 等）共用的常量与工具集中在此，
避免重复实现：临时下载目录、ffmpeg 能力探测、文件名清洗。

本模块不依赖任何具体解析器，处于依赖链末端，可被安全导入（无循环依赖风险）。
"""

from __future__ import annotations

import re
import shutil
import tempfile
from functools import lru_cache
from pathlib import Path

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
