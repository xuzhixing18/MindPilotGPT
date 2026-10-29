"""QQ音乐专用解析器 —— 链接归一化 + MV 元数据修正，媒体抓取仍委托 yt-dlp。

背景：QQ音乐分享链接形态多样，而 yt-dlp 内置提取器只认经典路由
    https://y.qq.com/n/ryqq/songDetail/<songmid>（base62 mid）。
实测确认（2026-10）：
- 老式播放页 i.y.qq.com/v8/playsong.html?songid=<数字> 会 302 到
  y.qq.com/n/ryqq_v2/songDetail/<数字>；ryqq_v2 路由与经典 ryqq 路由是同一页面，
  但 yt-dlp 只匹配经典路由；
- 音乐详情 API 只认 songmid（数字 songid 直喂返回 404），纯数字 songid 必须
  先换回 songmid：老接口 fcg_play_single_song.fcg 与 v2 页面 __INITIAL_DATA__
  都能拿到 mid；
- 分享短链 */y.qq.com/base/fcgi-bin/u（c/c6/m 等子域名，如
  c6.y.qq.com/base/fcgi-bin/u?__=xxx）302 到真实详情页；
- MV 的 format 缺 vcodec/acodec/height，需补齐才能保留 3 档清晰度。

职责边界：
1. 友好报错与 URL 归一化：
   - 壳页（/n/ryqq_v2/player 等）URL 不含内容 ID，任何解析器都无法处理，直接给中文指引；
   - i.y.qq.com 老式播放页（playsong.html）按 songid/songmid 参数换经典详情页；
   - ryqq_v2 路由改写为经典 ryqq；纯数字 songid 经老接口/页面数据换 songmid；
   - 分享短链先跟随 302 重定向拿到真实详情页；
   - 专辑/歌单/歌手/榜单是「列表型」URL，单链接模型不支持，给明确提示；
   - 老式 /n/yqq/mv/xxx.html 链接改写为现行 ryqq 路由。
2. MV：补齐缺失的编码/分辨率字段保留 3 档清晰度，未指定 format_id 时默认最高档。
3. 登录态：COOKIE_FILE / COOKIES_FROM_BROWSER 注入（VIP 歌曲需本人登录态，
   只认 entitlement，不碰任何加密文件）；未配置登录态时对 VIP 付费歌曲与失效
   MV 提前给中文指引，把 yt-dlp 的英文报错翻译成可操作的提示。
"""

from __future__ import annotations

import json
import re
import urllib.request
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

from yt_dlp.utils import js_to_json

from backend.downloader import common, generic

# 解析器接管的主机：y.qq.com（PC 详情页）/ c.y.qq.com（分享短链与老接口）
# / m.y.qq.com（移动端短链）/ i.y.qq.com（老式 H5 播放页 playsong.html）。
# 实际分享还会出现子域名变体：c6/c7.y.qq.com 短链、i2/i3.y.qq.com 移动播放页，
# 因此在 can_handle / _normalize 中按后缀匹配泛化处理。
_HOSTS = ("y.qq.com", "c.y.qq.com", "m.y.qq.com", "i.y.qq.com")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_SHELL_RE = re.compile(r"^/n/ryqq(?:_v2)?/player(?:$|[/?#])|^/portal/player\.html(?:$|[?#])")
_V2_PATH_RE = re.compile(r"^/n/ryqq_v2/")
_MV_RE = re.compile(r"^/n/ryqq/mv/[0-9A-Za-z]+(?:$|[/?#])")
_LIST_RE = re.compile(r"^/n/ryqq/(albumDetail|playlist|singer|toplist)/")
_SHARE_RE = re.compile(r"^/base/fcgi-bin/u")
_PLAYSONG_RE = re.compile(r"^/v8/playsong\.html(?:$|[?#])")
# i/i2/i3.y.qq.com 移动播放页主机（playsong.html 及其变体路由）
_I_MOBILE_RE = re.compile(r"^i\d*\.y\.qq\.com$")
_OLD_STYLE_RE = re.compile(r"^/n/yqq/(mv|song)/([0-9A-Za-z]+)\.html$")
# 纯数字 songid（≤12 位；songmid 为 14 位 base62，两者不会混淆）
_NUMERIC_SONG_RE = re.compile(r"^/n/ryqq/songDetail/(\d{1,12})(?:$|[/?#])")
# 归一化后的经典详情页/MV 页路径（付费预检用）
_SONG_DETAIL_RE = re.compile(r"^/n/ryqq/songDetail/([0-9A-Za-z]+)$")
_MV_DETAIL_RE = re.compile(r"^/n/ryqq/mv/([0-9A-Za-z]+)$")
_INIT_DATA_RE = re.compile(r"window\.__INITIAL_DATA__\s*=")

SHELL_HINT = (
    "该链接是QQ音乐播放器壳页，不含歌曲信息，无法解析。请复制歌曲详情页"
    "（y.qq.com/n/ryqq/songDetail/...）或 MV 页（y.qq.com/n/ryqq/mv/...）的链接。"
)
LIST_HINT = "QQ音乐专辑/歌单/歌手/榜单链接暂不支持，请复制单曲详情页或 MV 页链接。"
SHARE_HINT = "无法打开该QQ音乐分享短链，请用浏览器打开后复制落地页链接重试。"
IPAGE_HINT = (
    "无法解析该QQ音乐链接，请复制歌曲详情页（y.qq.com/n/ryqq/songDetail/...）"
    "或含歌曲ID的分享链接（i.y.qq.com/v8/playsong.html?songid=...）重试。"
)
GONE_HINT = "该QQ音乐歌曲不存在或已下架。"
MV_GONE_HINT = "该链接对应的QQ音乐MV不存在或已下架，无法解析。"
MV_BLOCK_HINT = (
    "该QQ音乐MV未返回可播放地址（可能为VIP付费内容或已下架）。"
    "如为VIP内容，请在 .env 中配置 COOKIE_FILE 或 COOKIES_FROM_BROWSER 登录态后重试。"
)

# MV format_id 档位：9814/9824/9834 = 标清/高清/超清（个位 4 同编码家族，十位为档）
_MV_TIER = {"9814": 1, "9824": 2, "9834": 3}
_MV_TIER_LABEL = {1: "标清", 2: "高清", 3: "超清"}


def can_handle(url: str) -> bool:
    """QQ音乐各域名内容链接（子域名后缀匹配，防 i.y.qq.com.evil.com）。"""
    host = (urlsplit(url).hostname or "").lower()
    return host in _HOSTS or any(host.endswith(f".{h}") for h in _HOSTS)


def _normalize(url: str) -> str:
    """壳页/列表页给中文报错；i.y.qq.com 参数换 mid；ryqq_v2 改写；短链跟随重定向。"""
    parts = urlsplit(url)
    path = parts.path or ""
    if _SHELL_RE.match(path):
        raise ValueError(SHELL_HINT)
    host = (parts.hostname or "").lower()
    if _I_MOBILE_RE.match(host):
        return _resolve_i_mobile(parts, url)
    new_path = _V2_PATH_RE.sub("/n/ryqq/", path, count=1)  # ryqq_v2 新路由 → 经典 ryqq
    if new_path != path:
        parts = parts._replace(path=new_path, query="", fragment="")
        url = urlunsplit(parts)
        path = new_path
    if _LIST_RE.match(path):
        raise ValueError(LIST_HINT)
    old = _OLD_STYLE_RE.match(path)
    if old:
        kind = "mv" if old.group(1) == "mv" else "songDetail"
        return urlunsplit((parts.scheme, parts.netloc, f"/n/ryqq/{kind}/{old.group(2)}", "", ""))
    if _SHARE_RE.match(path) and (host == "y.qq.com" or host.endswith(".y.qq.com")):
        return _resolve_share_redirect(url)
    if _NUMERIC_SONG_RE.match(path):
        return _resolve_song_mid(url)
    return url


def _fcu_json(modules: dict[str, Any]) -> dict[str, Any] | None:
    """POST u.y.qq.com/cgi-bin/musicu.fcg 批量接口（匿名），失败返回 None（尽力而为）。"""
    body = json.dumps(
        {"comm": {"cv": 0, "ct": 24, "format": "json", "uin": 0}, **modules},
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://u.y.qq.com/cgi-bin/musicu.fcg",
        data=body,
        headers={
            "User-Agent": _UA,
            "Content-Type": "application/json",
            "Referer": "https://y.qq.com/",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8", errors="ignore"))
    except (OSError, ValueError):
        return None


def _has_anonymous_purl(mid: str, media_mid: str) -> bool:
    """VIP 歌曲匿名 GetVkey 是否仍有可播放流（个别歌曲有试听 purl，则不拦截）。"""
    if not media_mid:
        return False
    data = _fcu_json({"req_1": {
        "module": "vkey.GetVkeyServer",
        "method": "CgiGetVkey",
        "param": {
            "guid": "10000",
            "songmid": [mid],
            "songtype": [0],
            "uin": "0",
            "loginflag": 1,
            "platform": "20",
            "filename": [f"M500{media_mid}.mp3", f"C400{media_mid}.m4a"],
        },
    }})
    if not data:
        return False
    for item in ((data.get("req_1") or {}).get("data") or {}).get("midurlinfo") or []:
        if item.get("purl"):
            return True
    return False


def _vip_hint(title: str = "") -> str:
    head = f"该歌曲《{title}》" if title else "该歌曲"
    return (
        f"{head}为QQ音乐VIP付费内容，未配置登录态时无法解析或下载。"
        "请在本项目 .env 中配置 COOKIE_FILE（Netscape 格式 cookies.txt 路径）"
        "或 COOKIES_FROM_BROWSER（如 chrome），使用您本人的QQ音乐登录态后重试。"
    )


def _check_paywall(url: str) -> None:
    """预检 VIP 付费歌曲与失效 MV，把 yt-dlp 的英文报错提前翻译成中文指引。

    背景：VIP 歌曲匿名解析被 yt-dlp 以「This video is only available for
    registered users」拒绝，MV 缺失/下架报「No video formats found」，
    前端只能看到生英文。解析/下载/转写各路径都经 normalize 汇聚于此。
    已配置登录态（COOKIE_FILE / COOKIES_FROM_BROWSER）时不拦截，交给
    yt-dlp 带 Cookie 尝试；接口异常也放行（尽力而为，避免误伤）。
    """
    if common.cookies_configured():
        return
    parts = urlsplit(url)
    if (parts.hostname or "").lower() != "y.qq.com":
        return
    song = _SONG_DETAIL_RE.match(parts.path or "")
    if song:
        data = _fcu_json({"info": {
            "module": "music.pf_song_detail_svr",
            "method": "get_song_detail_yqq",
            "param": {"song_mid": song.group(1), "song_type": 0},
        }})
        if not data:
            return
        if (data.get("info") or {}).get("code") != 0:
            return  # 歌曲不存在等交由提取器报错
        track = ((data.get("info") or {}).get("data") or {}).get("track_info") or {}
        if not track:
            return
        if not (track.get("pay") or {}).get("pay_play"):
            return
        media_mid = (track.get("file") or {}).get("media_mid") or ""
        if _has_anonymous_purl(song.group(1), media_mid):
            return  # 匿名仍有试听流，与 yt-dlp 行为一致，不拦截
        raise ValueError(_vip_hint(str(track.get("title") or "")))
    mv = _MV_DETAIL_RE.match(parts.path or "")
    if mv:
        data = _fcu_json({"mvInfo": {
            "module": "music.video.VideoData",
            "method": "get_video_info_batch",
            "param": {"vidlist": [mv.group(1)],
                      "required": ["vid", "code", "play_forbid_reason"]},
        }})
        if not data:
            return
        entry = ((data.get("mvInfo") or {}).get("data") or {}).get(mv.group(1)) or {}
        # code!=0 表示该 vid 不是有效视频；地理限制（play_forbid_reason==3）交给 yt-dlp 报 geo 错误
        if (entry.get("code") or 0) != 0 and entry.get("play_forbid_reason") != 3:
            raise ValueError(MV_GONE_HINT)


def _map_generic_error(exc: ValueError) -> ValueError:
    """把 yt-dlp 的英文错误翻译成中文指引（不匹配则原样返回）。"""
    msg = str(exc)
    if "registered users" in msg:
        return ValueError(_vip_hint())
    if "No video formats found" in msg:
        return ValueError(MV_BLOCK_HINT)
    return exc


def normalize(url: str) -> str:
    """对外统一入口（downloader.normalize() 调度）：归一化 + 付费/失效内容预检。"""
    url = _normalize(url)
    _check_paywall(url)
    return url


def _canonical(path: str) -> str:
    """构造经典详情页 URL：yt-dlp 提取器只认 y.qq.com 主机 + ryqq 路由。"""
    return urlunsplit(("https", "y.qq.com", path, "", ""))


def _resolve_i_mobile(parts, url: str) -> str:
    """i/i2/i3.y.qq.com 移动播放页：playsong.html 或带 songid/songmid 参数的页面 → 经典详情页。"""
    params = parse_qs(parts.query)
    songmid = (params.get("songmid") or [""])[0].strip()
    if songmid:
        return _canonical(f"/n/ryqq/songDetail/{songmid}")
    songid = (params.get("songid") or [""])[0].strip()
    if songid.isdigit():
        mid = _songid_to_mid(songid)
        if not mid:
            raise ValueError(GONE_HINT)
        return _canonical(f"/n/ryqq/songDetail/{mid}")
    if _PLAYSONG_RE.match(parts.path or ""):
        # 无参数播放页：跟随服务端 302 再归一化（大概率落到壳页/详情页）
        return _resolve_share_redirect(url)
    raise ValueError(IPAGE_HINT)


def _resolve_share_redirect(url: str) -> str:
    """跟随分享短链的 302 跳转拿到真实详情页 URL（离线测试可打桩 urlopen）。"""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(request, timeout=15) as resp:
            final = resp.geturl()
    except OSError as exc:
        raise ValueError(f"无法打开QQ音乐分享链接：{exc}") from exc
    if not can_handle(final):
        raise ValueError(SHARE_HINT)
    return _normalize(final)  # 落地页仍可能是壳页/列表页/数字 songid，再归一化一次


def _extract_js_object(html: str, start: int) -> str:
    """从 start 处取一个平衡的大括号对象字面量（页面内嵌 JS 数据）。"""
    index = start
    while index < len(html) and html[index].isspace():
        index += 1
    if index >= len(html) or html[index] != "{":
        return ""
    depth, in_str, escaped = 0, False, False
    for cursor in range(index, len(html)):
        ch = html[cursor]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return html[index:cursor + 1]
    return ""


def _songid_to_mid(songid: str) -> str | None:
    """数字 songid → songmid：先试老接口 JSON（轻量），失败再抓 v2 页面兜底。"""
    try:
        api = (f"https://c.y.qq.com/v8/fcg-bin/fcg_play_single_song.fcg"
               f"?songid={songid}&format=json")
        request = urllib.request.Request(
            api, headers={"User-Agent": _UA, "Referer": "https://y.qq.com/"})
        with urllib.request.urlopen(request, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
        entry = (data.get("data") or [{}])[0] or {}
        mid = str(entry.get("mid") or "").strip()
        if mid:
            return mid
    except (OSError, ValueError):
        pass  # 老接口不可用/异常时走页面兜底
    return _mid_from_v2_page(songid)


def _mid_from_v2_page(songid: str) -> str | None:
    """抓 v2 详情页 __INITIAL_DATA__ 换 songmid（老接口失效时的兜底）。"""
    url = f"https://y.qq.com/n/ryqq_v2/songDetail/{songid}"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(request, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except OSError:
        return None
    marker = _INIT_DATA_RE.search(html)
    if not marker:
        return None
    try:
        data = json.loads(js_to_json(_extract_js_object(html, marker.end())))
    except ValueError:
        return None
    if data.get("notFound"):
        return None
    return ((data.get("detail") or {}).get("mid")
            or ((data.get("songList") or [{}])[0] or {}).get("mid") or None)


def _resolve_song_mid(url: str) -> str:
    """纯数字 songid 的经典路由 → 换回 songmid 的经典详情页 URL。"""
    parts = urlsplit(url)
    match = _NUMERIC_SONG_RE.match(parts.path or "")
    if not match:
        return url
    mid = _songid_to_mid(match.group(1))
    if not mid:
        raise ValueError(GONE_HINT)
    return _canonical(f"/n/ryqq/songDetail/{mid}")


def _mv_tier(format_id: str | None) -> int:
    return _MV_TIER.get(format_id or "", 0)


def _patch_mv_formats(info: dict[str, Any]) -> None:
    """QQMusicVideo 的格式缺 vcodec/acodec/分辨率，补齐后才能保住 3 档清晰度。

    注意：yt-dlp 后处理会给缺失字段显式填 None（键存在、值为 None），
    因此不能用 setdefault，必须显式覆盖假值。
    """
    if info.get("extractor_key") != "QQMusicVideo":
        return
    for fmt in info.get("formats") or []:
        if not fmt.get("vcodec"):
            fmt["vcodec"] = "avc1"
        if not fmt.get("acodec"):
            fmt["acodec"] = "aac"
        tier = _mv_tier(fmt.get("format_id"))
        if not fmt.get("resolution"):
            fmt["resolution"] = _MV_TIER_LABEL.get(tier, "MV")
        fmt["preference"] = tier


def _best_mv_format_id(url: str) -> str | None:
    """MV 未指定清晰度时选最高档（离线测试可打桩 generic.extract_raw）。"""
    info = generic.extract_raw(url)
    _patch_mv_formats(info)
    best = None
    best_key = (-1, -1)
    for fmt in info.get("formats") or []:
        key = (fmt.get("preference") or 0, fmt.get("filesize") or 0)
        if key > best_key:
            best_key = key
            best = fmt.get("format_id")
    return best


def extract_info(url: str) -> dict[str, Any]:
    """解析视频信息（对应 /api/info）：归一化后走通用解析，MV 先补元数据。"""
    url = normalize(url)
    try:
        if _MV_RE.match(urlsplit(url).path or ""):
            info = generic.extract_raw(url)
            _patch_mv_formats(info)
            return generic.build_info(info, url)
        return generic.extract_info(url)
    except ValueError as exc:
        raise _map_generic_error(exc) from exc


def download(url: str, format_id: str | None = None) -> dict[str, Any]:
    """下载（对应 /api/download）：MV 未指定清晰度时默认最高档。"""
    url = normalize(url)
    try:
        if _MV_RE.match(urlsplit(url).path or "") and not format_id:
            format_id = _best_mv_format_id(url)
        return generic.download(url, format_id)
    except ValueError as exc:
        raise _map_generic_error(exc) from exc
