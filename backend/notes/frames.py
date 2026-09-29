"""后端抽帧：B 站 / YouTube（跨域 iframe，前端 canvas 抓帧被 taint）的截图通道。

与前端 Canvas 通道（抖音等原生 <video> 平台）互补，合起来实现 P0 三平台一致。
三级降级链（capture 编排）：
1. 本地快通道：**只查**进程内流播缓存（用户刚经 /api/stream 在线播放过的视频），
   命中直接 ffmpeg 抽帧；
2. 远程直连抽帧：yt-dlp 解析低分辨率视频流直链 → ffmpeg 用 -ss 前置在 HTTP Range
   下只拉取 t 附近片段抽帧。B站/YouTube 用官方 iframe 播放器，视频流浏览器直连
   CDN、从不经过后端，本地缓存永不命中；此通道让 iframe 平台也能截到正在播放
   的画面，且**不下载整片**（既有「绝不为截一帧触发整片下载」教训）；
3. 无 ffmpeg / 两级皆失败：回退 yt-dlp 封面（poster，非当前帧），返回
   frame_type=poster 让前端标注。

SSRF 防护：仅接受 http/https、拒绝内网与本机地址（对齐既有 SSRF 踩坑规范）；
``t`` 限定 [0, 24h]。
"""

from __future__ import annotations

import ipaddress
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from backend.downloader.common import ffmpeg_path
from backend.notes import config, images
from backend.notes.errors import ValidationError

log = logging.getLogger(__name__)

_FFMPEG_TIMEOUT = 25   # 单帧抽取的硬超时（秒）：-ss 前置 + 流式输入通常 <3s


def validate_frame_url(url: str) -> str:
    """抽帧 URL 安全校验：仅 http/https、拒绝内网与本机（SSRF 防线）。"""
    cleaned = (url or "").strip()
    if not cleaned:
        raise ValidationError("视频链接不能为空。")
    try:
        parts = urlsplit(cleaned)
    except ValueError as exc:
        raise ValidationError("视频链接不合法。") from exc
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValidationError("仅支持 http/https 链接。")
    host = (parts.hostname or "").strip().lower()
    if not host:
        raise ValidationError("视频链接不合法。")
    if _is_private_host(host):
        raise ValidationError("该链接指向内网或本机地址，不允许抽帧。")
    return cleaned


# 域名解析后判定的显式内网网段（不含 198.18.0.0/15 fake-ip 段：
# Python 3.11+ 的 is_private 会把它判 True，但它是 Clash/Surge 等代理的默认网段，
# 目标用户开代理访问 B站/YouTube 是常态，误拒必现；攻击者直接给字面 IP 时
# 仍走上面的全类别从严判定，这里的宽松只影响「域名解析到 fake-ip」的正常用户）
_DOMAIN_BLOCKED_NETS = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",
    "::/128", "::1/128", "fe80::/10", "fc00::/7",
))


def _is_private_host(host: str) -> bool:
    """判定 host 是否内网/本机。

    - 字面 IP：全类别拒绝（is_private/is_loopback/is_link_local/is_reserved，
      含 169.254.169.254 云元数据与 198.18 假 IP 直填）；
    - 域名：解析 A/AAAA 后按显式内网网段清单拒绝（见 _DOMAIN_BLOCKED_NETS；
      fake-ip 代理网段故意放过）。DNS rebinding 下解析判定可被双解析绕过，
      防护增益有限，优先保可用性。
    """
    import socket

    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
    except ValueError:
        pass   # 是域名而非字面 IP

    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            infos = socket.getaddrinfo(host, None, family)
        except OSError:
            continue
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if any(ip in net for net in _DOMAIN_BLOCKED_NETS):
                return True
    return False


def extract_frame(source: Path, t: float) -> bytes | None:
    """ffmpeg 从已缓存文件抽 t 秒处的帧（-ss 前置快速定位）。

    ``Popen + timeout``：绝不 subprocess.run 长等（防事件循环/线程池被长任务占满）；
    本函数由同步 def 端点调用，Starlette 会放进线程池执行。
    """
    ffmpeg = ffmpeg_path()
    if not ffmpeg or not source.exists():
        return None
    with tempfile.TemporaryDirectory(prefix="mp_note_frame_") as td:
        out = Path(td) / "frame.jpg"
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-ss", f"{max(0.0, t):.2f}",   # 输入前定位：快速 seek，不整片解码
            "-i", str(source),
            "-frames:v", "1", "-q:v", "3",
            "-y", str(out),
        ]
        return _ffmpeg_grab(cmd, out, f"source={source.name} t={t:.1f}")


def _ffmpeg_grab(cmd: list[str], out: Path, what: str) -> bytes | None:
    """执行 ffmpeg 抽帧命令（硬超时 + kill），成功返回帧字节。"""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            proc.wait(timeout=_FFMPEG_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            log.warning("[frames] ffmpeg 抽帧超时 %s", what)
            return None
    except OSError as exc:
        log.error("[frames] ffmpeg 启动失败: %s", exc)
        return None
    if proc.returncode != 0 or not out.exists():
        return None
    try:
        return out.read_bytes()
    except OSError:
        return None


def _resolve_frame_stream(url: str) -> tuple[str, dict[str, str]] | None:
    """yt-dlp 解析视频流直链与请求头（抽帧只取低分辨率，省带宽抢时间）。

    返回 (直链, 请求头)；解析失败（平台限流/地区限制等）返回 None 静默降级。
    """
    try:
        from yt_dlp import YoutubeDL

        opts = {
            "quiet": True, "no_warnings": True, "noplaylist": True,
            "socket_timeout": 15, "retries": 1, "skip_download": True,
            # 抽帧不需要音频与高清：优先 ≤480p 纯视频流，再退化到任意视频流
            "format": "bv*[height<=480]/b[height<=480]/bv*/b",
        }
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        headers = {**(info.get("http_headers") or {})}
        for fmt in info.get("formats") or []:
            if fmt.get("url") and fmt.get("vcodec") not in (None, "none"):
                headers.update(fmt.get("http_headers") or {})
                return fmt["url"], headers
        if info.get("url"):   # 单格式直链（无 formats 列表）
            return info["url"], headers
        return None
    except Exception as exc:  # noqa: BLE001 解析失败属常态（限流/地域），静默走封面降级
        log.info("[frames] 直链解析失败 url=%s: %s", url, exc)
        return None


def extract_frame_remote(url: str, t: float) -> bytes | None:
    """远程直连抽帧：ffmpeg 对直链 -ss 前置 seek，只拉 t 附近片段（不下载整片）。

    服务端不支持 Range 或平台限流时由硬超时截断，返回 None 交给封面兜底；
    本函数由同步 def 端点调用，Starlette 会放进线程池执行。
    """
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        return None
    resolved = _resolve_frame_stream(url)
    if not resolved:
        return None
    stream_url, headers = resolved
    with tempfile.TemporaryDirectory(prefix="mp_note_frame_") as td:
        out = Path(td) / "frame.jpg"
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-rw_timeout", "15000000"]
        lines = "".join(f"{k}: {v}\r\n" for k, v in headers.items() if k and v)
        if lines:
            cmd += ["-headers", lines]   # Referer/UA 等：B站 CDN 无 Referer 会 403
        ua = next((v for k, v in headers.items() if k.lower() == "user-agent"), None)
        if ua:
            cmd += ["-user_agent", ua]
        cmd += [
            "-ss", f"{max(0.0, t):.2f}",
            "-i", stream_url,
            "-frames:v", "1", "-q:v", "3",
            "-y", str(out),
        ]
        return _ffmpeg_grab(cmd, out, f"remote t={t:.1f} url={url}")


def extract_poster(url: str) -> bytes | None:
    """回退封面：yt-dlp 解析封面直链并下载（非当前帧）。失败返回 None。"""
    try:
        from yt_dlp import YoutubeDL

        opts = {
            "quiet": True, "no_warnings": True, "noplaylist": True,
            "socket_timeout": 15, "retries": 1, "skip_download": True,
        }
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        thumb = info.get("thumbnail") or ""
        if not thumb:
            return None
        import urllib.request

        req = urllib.request.Request(thumb, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:   # noqa: S310 已过白名单校验
            data = resp.read(config.note_image_max_bytes() + 1)
        return data if data and len(data) <= config.note_image_max_bytes() else None
    except Exception as exc:  # noqa: BLE001 封面兜底失败静默：上层再降级提示
        log.info("[frames] 封面兜底失败 url=%s: %s", url, exc)
        return None


def capture(
    url: str,
    t: float,
    cached_file: Path | None,
) -> dict[str, Any]:
    """抽帧编排：校验 → 本地流播缓存抽帧 → 远程直连抽帧 → 封面兜底。

    :param cached_file: 进程内流播缓存命中的文件路径（由 main.py 注入，可能为 None）
    :return: {kind: frame|poster, data, mime}；三者皆失败抛 ValidationError
    """
    validate_frame_url(url)
    if not 0 <= float(t) <= 24 * 3600:
        raise ValidationError("截图时间点越界。")

    if cached_file is not None:
        data = extract_frame(cached_file, float(t))
        if data:
            sniffed = images.sniff_image(data)
            if sniffed:   # ffmpeg 输出 jpg，正常必命中；防御性再验一次魔数
                return {"kind": "frame", "data": data, "mime": sniffed[1]}

    # iframe 平台（B站/YouTube）本地缓存必不命中：远程直连只拉 t 附近片段抽帧
    data = extract_frame_remote(url, float(t))
    if data:
        sniffed = images.sniff_image(data)
        if sniffed:
            return {"kind": "frame", "data": data, "mime": sniffed[1]}

    poster = extract_poster(url)
    if poster:
        sniffed = images.sniff_image(poster)
        if sniffed:
            return {"kind": "poster", "data": poster, "mime": sniffed[1]}

    raise ValidationError("暂无可用的画面：远程抽帧与封面均失败，请稍后重试。")
