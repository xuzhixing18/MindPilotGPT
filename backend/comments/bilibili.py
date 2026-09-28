"""B 站评论抓取：BV/av → aid → ``x/v2/reply/wbi/main``（WBI 签名，mode=3 按热度）。

B 站评论不在 yt-dlp 支持范围内，走公开 web 接口：
1) ``x/web-interface/view`` 由 bvid/aid 拿到 aid 与标题（同时兼容 b23.tv 短链）；
2) ``x/frontend/finger/spi`` 领取 buvid3/buvid4 访客 Cookie——**无 Cookie 的裸请求
   会被风控拦截（code=-352、data 为空）**，这是「部分视频抓不到评论」的根因；
3) ``x/v2/reply/wbi/main``（WBI 签名 + ``mode=3`` 热门/按赞）取首页评论，签名链路
   异常时回退旧端点 ``x/v2/reply/main``；命中风控码时换新指纹重试一次。

读评论无需登录；字段归一到 ``{author, text, likes, time}``。
接口 ``code != 0`` 时抛 ``CommentsError`` 如实上报（含 code），**不得**伪装成
「平台不支持/无评论」——只有 code=0 且确无评论才由门面判 NotSupported。
"""

from __future__ import annotations

import hashlib
import re
import time
import urllib.parse
from typing import Any

import requests

from backend.comments.errors import CommentsError, CommentsNotSupportedError

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}
_TIMEOUT = 20

# 风控拦截码：-352 风控校验失败 / -412 请求被拒绝（多因无指纹 Cookie 或频率）
_RISK_CODES = {-352, -412}

# WBI mixin key 缓存（key 按天轮换，缓存 30 分钟足够且避免每次请求都调 nav）
_WBI_TTL = 1800

# WBI 参数混淆置换表（B 站前端固定算法）
_MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
    20, 34, 44, 52,
]

_sess: requests.Session | None = None
_wbi_cache: tuple[float, str] | None = None  # (过期时间戳, mixin_key)


def can_handle(url: str) -> bool:
    u = (url or "").lower()
    return "bilibili.com" in u or "b23.tv" in u


def _session() -> requests.Session:
    """进程级会话（复用连接 + 携带访客 Cookie）；首次创建时领取 buvid。"""
    global _sess
    if _sess is not None:
        return _sess
    s = requests.Session()
    s.headers.update(_HEADERS)
    try:  # 尽力领取访客指纹：失败则无 Cookie 继续，风控失败由上层如实报错
        resp = s.get("https://api.bilibili.com/x/frontend/finger/spi", timeout=15)
        d = (resp.json() or {}).get("data") or {}
        if d.get("b_3"):
            s.cookies.set("buvid3", d["b_3"], domain=".bilibili.com")
            s.cookies.set("buvid4", d.get("b_4") or "", domain=".bilibili.com")
    except (requests.RequestException, ValueError):
        pass
    _sess = s
    return s


def _reset_session() -> None:
    """命中风控后更换访客指纹（重新领取 buvid）再试。"""
    global _sess
    _sess = None


def _get_json(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """统一 GET + JSON 解析（测试可 monkeypatch 本函数隔离网络）。"""
    try:
        resp = _session().get(url, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json() or {}
    except requests.RequestException as exc:
        raise CommentsError(f"B 站接口请求失败：{exc}") from exc
    except ValueError as exc:  # 非 JSON 响应：多为风控拦截页
        raise CommentsError(f"B 站接口响应非 JSON（可能被风控拦截）：{exc}") from exc


def _mixin_key() -> str | None:
    """WBI mixin key：nav 的 img/sub key 经固定置换表混淆取前 32 位（带缓存）。"""
    global _wbi_cache
    now = time.time()
    if _wbi_cache and _wbi_cache[0] > now:
        return _wbi_cache[1]
    try:
        j = _get_json("https://api.bilibili.com/x/web-interface/nav")
    except CommentsError:
        return _wbi_cache[1] if _wbi_cache else None  # nav 失败：用旧缓存或放弃签名
    wbi = ((j.get("data") or {}).get("wbi_img") or {})
    img = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
    sub = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
    if len(img) != 32 or len(sub) != 32:  # 防御：nav 结构异常时不签名，走旧端点
        return None
    key = "".join((img + sub)[i] for i in _MIXIN_TAB)[:32]
    _wbi_cache = (now + _WBI_TTL, key)
    return key


def _sign(params: dict[str, Any], mixin_key: str) -> dict[str, Any]:
    """WBI 签名：补 wts → 按键排序并剔除特殊字符 → md5(query + mixin_key) = w_rid。"""
    signed = {
        k: "".join(ch for ch in str(v) if ch not in "!'()*")
        for k, v in sorted(params.items())
    }
    signed["wts"] = int(time.time())
    query = urllib.parse.urlencode(signed)
    signed["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return signed


def _resolve(url: str) -> tuple[int, str]:
    """解析出 (aid, title)；支持 b23.tv 短链、BV 号与 av 号。"""
    try:
        if "b23.tv" in url.lower():
            url = _session().get(url, timeout=15, allow_redirects=True).url
    except requests.RequestException as exc:
        raise CommentsError(f"B 站短链解析失败：{exc}") from exc

    params: dict[str, Any] = {}
    av = re.search(r"av(\d+)", url)
    bv = re.search(r"(BV[0-9A-Za-z]{10})", url)
    if av:
        params["aid"] = int(av.group(1))
    elif bv:
        params["bvid"] = bv.group(1)
    else:
        raise CommentsNotSupportedError("无法从链接中识别 B 站视频 ID。")

    data = (_get_json("https://api.bilibili.com/x/web-interface/view", params) or {}).get("data") or {}
    aid = data.get("aid")
    if not aid:
        raise CommentsNotSupportedError("未能获取 B 站视频 aid。")
    return int(aid), data.get("title") or ""


def _fetch_replies_once(aid: int, ps: int) -> tuple[int, str, dict[str, Any]]:
    """单轮尝试：WBI 新端点优先，非 0 码回退旧端点；返回 (code, message, data)。"""
    base: dict[str, Any] = {"type": 1, "oid": aid, "mode": 3, "ps": ps, "next": 0}
    key = _mixin_key()
    if key:
        j = _get_json("https://api.bilibili.com/x/v2/reply/wbi/main", _sign(base, key))
        if j.get("code") == 0:
            return 0, j.get("message") or "OK", j.get("data") or {}
    j = _get_json("https://api.bilibili.com/x/v2/reply/main", base)
    return int(j.get("code") or 0), j.get("message") or "", j.get("data") or {}


def _fetch_replies(aid: int, ps: int) -> tuple[int, str, dict[str, Any]]:
    """取评论首页；命中风控码时换访客指纹重试一次（仍失败则把 code 交给上层如实上报）。"""
    last: tuple[int, str, dict[str, Any]] = (0, "", {})
    for attempt in range(2):
        code, message, data = _fetch_replies_once(aid, ps)
        if code == 0 or code not in _RISK_CODES or attempt == 1:
            return code, message, data
        last = (code, message, data)
        _reset_session()
    return last


def fetch(url: str, limit: int) -> dict[str, Any]:
    """抓取评论并归一化为 {title, comments:[{author,text,likes,time}]}。"""
    aid, title = _resolve(url)
    code, message, data = _fetch_replies(aid, min(max(int(limit), 1), 49))
    if code != 0:
        raise CommentsError(
            f"B 站评论接口返回 code={code}：{message or '未知错误'}"
            "（多为风控拦截或接口变更，请稍后重试）"
        )

    comments: list[dict[str, Any]] = []
    seen: set[Any] = set()
    # 置顶/热门评论（top_replies）与列表评论（replies）合并去重，保证高赞不遗漏
    for r in (data.get("top_replies") or []) + (data.get("replies") or []):
        rpid = r.get("rpid")
        if rpid is not None and rpid in seen:
            continue
        seen.add(rpid)
        text = ((r.get("content") or {}).get("message") or "").strip()
        if not text:
            continue
        comments.append({
            "author": ((r.get("member") or {}).get("uname")) or "匿名",
            "text": text,
            "likes": int(r.get("like") or 0),
            "time": r.get("ctime"),
        })
    return {"title": title, "comments": comments}
