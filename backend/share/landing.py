"""分享落地页服务端渲染（/s/{code}）。

为什么必须 SSR：微信/Telegram/X 等爬虫只读 HTML 的 og meta 与正文，hash 路由的
SPA 对爬虫是空壳。另有两个微信兜底事实：无 JS-SDK 时微信「···」分享取**页面
title + 正文第一张图**——故 ``<title>`` 用视频标题、正文首图用封面代理图。

页面纯静态（无 JS 依赖）：匿名接收者无需登录即可读快照；CTA 跳 SPA 深链
（``#/v?url=&tab=``）解锁完整分析。所有动态内容全量转义（含 og 属性引号）。
"""

from __future__ import annotations

from html import escape
from typing import Any

KIND_LABELS = {
    "video": "视频",
    "summary": "AI 总结",
    "transcript": "字幕文本",
    "mindmap": "思维导图",
    "comments": "高赞评论",
    "qa": "AI 问答",
    "notes": "随手笔记",
}

_STYLE = """
body{margin:0;background:#f5f6f8;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;color:#0f172a}
.wrap{max-width:640px;margin:0 auto;padding:24px 16px 48px}
.brand{display:flex;align-items:center;gap:8px;font-size:13px;color:#64748b;padding:16px 0 0}
.brand i{width:10px;height:10px;border-radius:50%;background:#1677ff;display:inline-block}
.card{margin-top:12px;background:#fff;border-radius:20px;overflow:hidden;box-shadow:0 4px 24px rgba(15,23,42,.08)}
.cover{display:block;width:100%;aspect-ratio:16/9;object-fit:cover;background:#e2e8f0}
.body{padding:20px 20px 24px}
h1{margin:0;font-size:20px;line-height:1.4}
.meta{margin-top:8px;font-size:12px;color:#94a3b8}
.sec{margin-top:16px;font-size:13px;font-weight:600;color:#475569}
.sec-h{margin-top:22px;padding-top:14px;border-top:1px solid #e2e8f0;font-size:15px;font-weight:700;color:#0f172a}
.excerpt{margin:8px 0 0;font-size:14px;line-height:1.7;color:#334155;white-space:pre-wrap}
ul{margin:8px 0 0;padding-left:18px;font-size:14px;line-height:1.7;color:#334155}
.cm{padding:10px 0;border-bottom:1px solid #f1f5f9;font-size:14px;line-height:1.6;color:#334155}
.cm b{color:#0f172a;font-size:13px}
.cm .lk{color:#f43f5e;font-size:12px;margin-left:8px}
.qa{padding:10px 0;border-bottom:1px solid #f1f5f9;font-size:14px;line-height:1.6}
.qa .q{color:#1677ff;font-weight:600}
.qa .a{margin-top:4px;color:#334155}
.cta{display:block;margin-top:20px;background:#1677ff;color:#fff;text-align:center;text-decoration:none;
border-radius:14px;padding:12px 0;font-size:15px;font-weight:700}
.tip{margin-top:12px;font-size:12px;color:#94a3b8;text-align:center}
.gone{padding:64px 20px;text-align:center;font-size:14px;color:#64748b}
"""


def _og_image(share: dict[str, Any], base_url: str) -> str:
    thumb = share.get("thumb_url") or ""
    if not thumb:
        return ""
    return f"{base_url}{thumb}" if thumb.startswith("/") else thumb


def _section_body(kind: str, excerpt: str, payload: dict[str, Any]) -> str:
    """单载体正文 HTML（excerpt 已转义）；bundle 逐 section 复用同一渲染。"""
    excerpt = escape(excerpt or "")
    parts: list[str] = []

    bullets = payload.get("bullets") or []
    if kind in ("video", "summary") and bullets:
        parts.append('<div class="sec">关键要点</div><ul>' +
                     "".join(f"<li>{escape(b)}</li>" for b in bullets) + "</ul>")
    elif excerpt:
        label = {
            "transcript": "字幕节选",
            "notes": "笔记节选",
        }.get(kind, "AI 一句话总结")
        parts.append(f'<div class="sec">{label}</div><p class="excerpt">{excerpt}</p>')

    if kind == "mindmap":
        nodes = payload.get("top_nodes") or []
        if nodes:
            parts.append('<div class="sec">导图一级分支</div><ul>' +
                         "".join(f"<li>{escape(n)}</li>" for n in nodes) + "</ul>")
    elif kind == "comments":
        rows = payload.get("comments") or []
        if rows:
            parts.append('<div class="sec">热评节选</div>' + "".join(
                f'<div class="cm"><b>{escape(c.get("author") or "匿名")}</b>'
                f'<span class="lk">👍 {int(c.get("likes") or 0)}</span>'
                f'<div>{escape(c.get("text") or "")}</div></div>'
                for c in rows))
    elif kind == "qa":
        pairs = payload.get("qa") or []
        if pairs:
            parts.append('<div class="sec">问答节选</div>' + "".join(
                f'<div class="qa"><div class="q">问：{escape(p.get("q") or "")}</div>'
                f'<div class="a">答：{escape(p.get("a") or "")}</div></div>'
                for p in pairs))

    if not parts and excerpt:
        parts.append(f'<p class="excerpt">{excerpt}</p>')
    return "".join(parts)


def _section(share: dict[str, Any]) -> str:
    payload = share.get("payload") or {}
    if share["kind"] != "bundle":
        return _section_body(share["kind"], share.get("excerpt") or "", payload)
    # bundle：按 kinds 顺序逐段渲染，每段带内容小标题（分隔线升一级）
    sections = payload.get("sections") or {}
    parts: list[str] = []
    for k in payload.get("kinds") or []:
        sec = sections.get(k)
        if not isinstance(sec, dict):
            continue
        inner = _section_body(k, sec.get("excerpt") or "", sec)
        if inner:
            label = escape(KIND_LABELS.get(k, k))
            parts.append(f'<div class="sec-h">{label}</div>{inner}')
    return "".join(parts)


def render(share: dict[str, Any], base_url: str) -> str:
    """落地页 HTML（og meta + 首图兜底 + 快照正文 + CTA）。"""
    title = escape(share.get("title") or "一段值得看的视频")
    if share["kind"] == "bundle":
        names = [KIND_LABELS.get(k, k) for k in ((share.get("payload") or {}).get("kinds") or [])]
        kind_label = "AI 分析合辑 · " + "、".join(names) if names else "AI 分析合辑"
    else:
        kind_label = KIND_LABELS.get(share["kind"], "视频")
    excerpt = share.get("excerpt") or ""
    desc = escape((excerpt or f"MindPilot AI 解析：{share.get('title') or ''}")[:120])
    og_image = _og_image(share, base_url)
    cover = f'<img class="cover" src="{escape(share["thumb_url"])}" alt="封面" referrerpolicy="no-referrer"/>' \
        if share.get("thumb_url") else ""
    model = escape((share.get("payload") or {}).get("model_label") or "")
    meta_bits = [kind_label]
    if share.get("view_count"):
        meta_bits.append(f'{share["view_count"]} 次浏览')
    if model:
        meta_bits.append(f"{model} 生成")

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{title} · MindPilot AI 视频分析</title>
<meta property="og:title" content="{title}"/>
<meta property="og:description" content="{desc}"/>
<meta property="og:type" content="article"/>
<meta property="og:url" content="{escape(share.get('share_url') or '', quote=True)}"/>
{f'<meta property="og:image" content="{escape(og_image)}"/>' if og_image else ''}
<meta name="twitter:card" content="summary_large_image"/>
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
  <div class="brand"><i></i>MindPilot · AI 视频分析</div>
  <div class="card">
    {cover}
    <div class="body">
      <h1>{title}</h1>
      <div class="meta">{" · ".join(escape(m) for m in meta_bits)}</div>
      {_section(share)}
      <a class="cta" href="{escape(share.get('cta_url') or base_url, quote=True)}">打开 MindPilot 查看完整分析</a>
      <p class="tip">本页无需登录即可浏览；注册后解锁完整 AI 总结、思维导图与问答。</p>
    </div>
  </div>
</div>
</body>
</html>"""


def render_gone(headline: str, tip: str) -> str:
    """失效页（410 撤销 / 404 不存在共用骨架）。"""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{escape(headline)} · MindPilot</title>
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap"><div class="card"><div class="gone">
  <div style="font-size:16px;font-weight:700;color:#0f172a">{escape(headline)}</div>
  <p>{escape(tip)}</p>
</div></div></div>
</body>
</html>"""
