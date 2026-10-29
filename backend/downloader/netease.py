"""网易云音乐 yt-dlp 兼容补丁（非解析器，不注册 registry）。

yt-dlp 的 NetEaseMusicBaseIE 用明文 HTTP（http://music.163.com/api/）拉取歌曲
信息、歌词等元数据。实测该接口会偶发「RemoteDisconnected / Connection aborted」
服务端连接重置（瞬时窗口，几秒后自愈，http/https 均可能命中）。https 版接口经
实测返回等价数据，且不会被运营商/中间设备对明文 HTTP 的干扰波及，因此导入本模块
时把提取器基类的 _API_BASE 原地切换为 https（不修改 yt-dlp 源码，仅运行时修正）。

由 downloader 门面 __init__.py 导入，保证所有走 yt-dlp 的调用路径
（解析 / 下载 / 字幕 / 音频兜底）在服务启动后自动生效。
"""
from __future__ import annotations

from yt_dlp.extractor.neteasemusic import NetEaseMusicBaseIE

if not NetEaseMusicBaseIE._API_BASE.startswith("https://"):
    NetEaseMusicBaseIE._API_BASE = "https://music.163.com/api/"
