"""分享落地页服务端渲染（/s/{code}）。

为什么必须 SSR：微信/Telegram/X 等爬虫只读 HTML 的 og meta 与正文，hash 路由的
SPA 对爬虫是空壳。另有两个微信兜底事实：无 JS-SDK 时微信「···」分享取**页面
title + 正文第一张图**——故 ``<title>`` 用视频标题、正文首图用封面代理图。

页面纯静态（无 JS 依赖）：匿名接收者无需登录即可读快照；CTA 跳 SPA 深链
（``#/v?url=&tab=``）解锁完整分析。所有动态内容全量转义（含 og 属性引号）。

SEO 对齐 docs/SEO优化工作流.md：完整 TDK + canonical + OG/Twitter Card +
JSON-LD（Article）结构化数据；落地页是站内唯一可被收录的 SSR 内容页（长尾
流量入口，robots 显式 index,follow）；失效页（410/404）noindex 防收录残留。

GEO 对齐 docs/GEO优化入门指南.md：结论先行（one_line 首屏可见）、小节标题用
真实 heading 标签（h1>h2>h3 有序嵌套）、章节速览列表、生成日期新鲜度标记、
问答载体/合辑追加 FAQPage JSON-LD（与可见问答节选同一数据源，不虚构）。
"""

from __future__ import annotations

import json
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
.sec{margin:16px 0 0;font-size:13px;font-weight:600;color:#475569}
.sec-h{margin:22px 0 0;padding-top:14px;border-top:1px solid #e2e8f0;font-size:15px;font-weight:700;color:#0f172a}
.excerpt{margin:8px 0 0;font-size:14px;line-height:1.7;color:#334155;white-space:pre-wrap}
ul,ol{margin:8px 0 0;padding-left:18px;font-size:14px;line-height:1.7;color:#334155}
.ch-ts{color:#1677ff;font-variant-numeric:tabular-nums;margin-right:6px}
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


def _fmt_ts(value: Any) -> str:
    """秒数 → mm:ss / h:mm:ss（无时间戳的 AI 章节回空串，不造数据）。"""
    try:
        sec = int(float(value))
    except (TypeError, ValueError):
        return ""
    if sec < 0:
        return ""
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _section_body(kind: str, excerpt: str, payload: dict[str, Any], *, sub: bool = False) -> str:
    """单载体正文 HTML（excerpt 已转义）；bundle 逐 section 复用同一渲染。

    GEO：小节标题用真实 heading 标签（非 bundle 用 h2；bundle 已有 h2 分组，
    内部降为 h3，保证 h1>h2>h3 有序嵌套）；video/summary 载体「一句话结论」
    先行（excerpt 即 AI one_line，旧逻辑有 bullets 时会被吞掉）。
    """
    tag = "h3" if sub else "h2"

    def head(text: str) -> str:
        return f'<{tag} class="sec">{text}</{tag}>'

    excerpt = escape(excerpt or "")
    parts: list[str] = []

    if kind in ("video", "summary"):
        if excerpt:
            parts.append(head("一句话结论") + f'<p class="excerpt">{excerpt}</p>')
        bullets = payload.get("bullets") or []
        if bullets:
            parts.append(head("关键要点") + "<ul>" +
                         "".join(f"<li>{escape(b)}</li>" for b in bullets) + "</ul>")
        chs = payload.get("chapters") or []
        if chs:
            items = []
            for c in chs:
                ts = _fmt_ts(c.get("start"))
                prefix = f'<span class="ch-ts">{ts}</span>' if ts else ""
                items.append(f"<li>{prefix}{escape(c.get('title') or '')}</li>")
            parts.append(head("章节速览") + "<ol>" + "".join(items) + "</ol>")
    elif excerpt:
        label = {
            "transcript": "字幕节选",
            "notes": "笔记节选",
        }.get(kind, "AI 一句话总结")
        parts.append(head(label) + f'<p class="excerpt">{excerpt}</p>')

    if kind == "mindmap":
        nodes = payload.get("top_nodes") or []
        if nodes:
            parts.append(head("导图一级分支") + "<ul>" +
                         "".join(f"<li>{escape(n)}</li>" for n in nodes) + "</ul>")
    elif kind == "comments":
        rows = payload.get("comments") or []
        if rows:
            parts.append(head("热评节选") + "".join(
                f'<div class="cm"><b>{escape(c.get("author") or "匿名")}</b>'
                f'<span class="lk">👍 {int(c.get("likes") or 0)}</span>'
                f'<div>{escape(c.get("text") or "")}</div></div>'
                for c in rows))
    elif kind == "qa":
        pairs = payload.get("qa") or []
        if pairs:
            parts.append(head("问答节选") + "".join(
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
    # bundle：按 kinds 顺序逐段渲染，每段带内容小标题（分隔线升一级，h2>h3 嵌套）
    sections = payload.get("sections") or {}
    parts: list[str] = []
    for k in payload.get("kinds") or []:
        sec = sections.get(k)
        if not isinstance(sec, dict):
            continue
        inner = _section_body(k, sec.get("excerpt") or "", sec, sub=True)
        if inner:
            label = escape(KIND_LABELS.get(k, k))
            parts.append(f'<h2 class="sec-h">{label}</h2>{inner}')
    return "".join(parts)


def _faq_pairs(share: dict[str, Any]) -> list[tuple[str, str]]:
    """FAQPage 数据源：qa 载体或 bundle 的 qa 段（服务端已消毒的 {q,a} 对）。

    红线：JSON-LD 必须与页面可见内容一致——「问答节选」区块已逐条渲染同样的
    q/a，这里只做 Schema 映射，不新增任何页面上看不到的问答。
    """
    payload = share.get("payload") or {}
    if share["kind"] == "qa":
        pairs = payload.get("qa") or []
    elif share["kind"] == "bundle":
        pairs = ((payload.get("sections") or {}).get("qa") or {}).get("qa") or []
    else:
        pairs = []
    return [(str(p.get("q")), str(p.get("a")))
            for p in pairs if isinstance(p, dict) and p.get("q") and p.get("a")]


def _faq_jsonld(pairs: list[tuple[str, str]]) -> str:
    """FAQPage JSON-LD script 标签（GEO 技巧②）；无问答时回空串不占位。"""
    if not pairs:
        return ""
    ld = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": q,
             "acceptedAnswer": {"@type": "Answer", "text": a}}
            for q, a in pairs
        ],
    }
    return ('<script type="application/ld+json">'
            + json.dumps(ld, ensure_ascii=False).replace("</", "<\\/") + "</script>")


def _jsonld(share: dict[str, Any], title_plain: str, desc_plain: str,
            og_image: str, share_url: str) -> str:
    """Article 结构化数据（富摘要候选）；``</`` 转义防提前闭合 script 标签。"""
    created = share.get("created_at") or ""
    ld: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": title_plain[:110],
        "description": desc_plain,
        "inLanguage": "zh-CN",
        "author": {"@type": "Organization", "name": "MindPilot"},
        "publisher": {"@type": "Organization", "name": "MindPilot"},
        "mainEntityOfPage": share_url,
    }
    if og_image:
        ld["image"] = [og_image]
    if created:
        ld["datePublished"] = f"{created}Z"   # 库内为 naive UTC ISO 串
    return json.dumps(ld, ensure_ascii=False).replace("</", "<\\/")


def render(share: dict[str, Any], base_url: str) -> str:
    """落地页 HTML（完整 TDK + OG/Twitter + JSON-LD + 首图兜底 + 快照正文 + CTA）。"""
    title_plain = share.get("title") or "一段值得看的视频"
    title = escape(title_plain)
    if share["kind"] == "bundle":
        names = [KIND_LABELS.get(k, k) for k in ((share.get("payload") or {}).get("kinds") or [])]
        kind_label = "AI 分析合辑 · " + "、".join(names) if names else "AI 分析合辑"
    else:
        kind_label = KIND_LABELS.get(share["kind"], "视频")
    excerpt = share.get("excerpt") or ""
    # description 前 150 字放核心信息（Google 截断位），尾部行动号召提升点击率
    desc_plain = f"{(excerpt or title_plain)[:150]}——由 MindPilot AI 解析生成，无需登录即可浏览，点击查看完整分析。"
    desc = escape(desc_plain)
    share_url = share.get("share_url") or f"{base_url}/s/{share.get('code', '')}"
    og_image = _og_image(share, base_url)
    cover = f'<img class="cover" src="{escape(share["thumb_url"])}" alt="{escape(title_plain[:60])} 视频封面" referrerpolicy="no-referrer"/>' \
        if share.get("thumb_url") else ""
    model = escape((share.get("payload") or {}).get("model_label") or "")
    meta_bits = [kind_label]
    created_day = (share.get("created_at") or "")[:10]
    if created_day:
        meta_bits.append(f"生成于 {created_day}")   # 新鲜度标记（GEO 技巧⑤：明确时间锚点）
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
<meta name="description" content="{desc}"/>
<meta name="keywords" content="MindPilot,AI视频分析,视频总结,字幕提取,思维导图,高赞评论,AI问答"/>
<meta name="robots" content="index,follow"/>
<link rel="canonical" href="{escape(share_url, quote=True)}"/>
<meta property="og:title" content="{title}"/>
<meta property="og:description" content="{desc}"/>
<meta property="og:type" content="article"/>
<meta property="og:url" content="{escape(share_url, quote=True)}"/>
<meta property="og:locale" content="zh_CN"/>
<meta property="og:site_name" content="MindPilot"/>
{f'<meta property="og:image" content="{escape(og_image)}"/>' if og_image else ''}
<meta name="twitter:card" content="summary_large_image"/>
<meta name="twitter:title" content="{title}"/>
<meta name="twitter:description" content="{desc}"/>
{f'<meta name="twitter:image" content="{escape(og_image)}"/>' if og_image else ''}
<script type="application/ld+json">{_jsonld(share, title_plain, desc_plain, og_image, share_url)}</script>
{_faq_jsonld(_faq_pairs(share))}
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
    """失效页（410 撤销 / 404 不存在共用骨架）；noindex 防失效链接残留收录。"""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta name="robots" content="noindex,nofollow"/>
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
