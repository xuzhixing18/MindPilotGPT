"""seo 路由：站点级搜索引擎基础设施（规范见 docs/SEO优化工作流.md §5.3 / §7）。

- GET /robots.txt   爬虫抓取规则（放行全站与静态资源，屏蔽 /api/，声明 sitemap）
- GET /sitemap.xml  首页 + 全部未撤销公开分享落地页（/s/{code} 是站内唯一可收录
                    的内容页：SSR 全量正文，hash 路由子视图对爬虫不可见）
- GET / 与 /index.html  动态渲染 SPA 壳：把 index.html 中的 __SEO_BASE_URL__
                    占位符替换为站点绝对地址（canonical / og:url / JSON-LD 需要
                    绝对 URL，纯静态托管给不出，故由本路由接管；其余路径仍走
                    main.py 末尾的 StaticFiles）
- http_exception_handler  非 /api 路径的 404 返回品牌化 HTML 页（软 404 信号治理），
                    /api 路径与其余状态码保持 FastAPI 默认 JSON 语义
"""

from __future__ import annotations

from html import escape as html_escape
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend import share
from backend.seo import config

router = APIRouter(tags=["seo"])

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"


def _base_url(request: Request) -> str:
    """站点域名事实源：PUBLIC_BASE_URL 优先，未配置回退请求 base_url（与 share 同判据）。"""
    return config.public_base_url() or str(request.base_url).rstrip("/")


# --------------------------------------------------------------------------- #
# robots.txt / sitemap.xml
# --------------------------------------------------------------------------- #
@router.get("/robots.txt")
def robots_txt(request: Request) -> Response:
    """爬虫规则：只屏蔽 /api/（JSON 端点无收录价值且防爬虫打进业务接口）。

    不屏蔽 CSS/JS（工作流 §5.3：Google 需要渲染资源）；/s/ 落地页显式放行
    （公开分享本体即免费内容，是站点长尾流量的主要来源）。
    """
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /api/\n"
        "\n"
        f"Sitemap: {_base_url(request)}/sitemap.xml\n"
    )
    return Response(content=body, media_type="text/plain; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=3600"})


@router.get("/sitemap.xml")
def sitemap_xml(request: Request) -> Response:
    """XML sitemap：首页（daily/1.0）+ 未撤销公开分享（weekly/0.6，lastmod=创建时间）。"""
    base = _base_url(request)
    urls = [
        f"  <url><loc>{xml_escape(base)}/</loc><changefreq>daily</changefreq><priority>1.0</priority></url>"
    ]
    limit = config.sitemap_share_limit()
    if limit > 0:
        for row in share.store.share_list_public(limit):
            lastmod = f"<lastmod>{xml_escape(row['created_at'])}Z</lastmod>" if row.get("created_at") else ""
            urls.append(
                f"  <url><loc>{xml_escape(base)}/s/{xml_escape(row['code'])}</loc>"
                f"{lastmod}<changefreq>weekly</changefreq><priority>0.6</priority></url>"
            )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls) + "\n</urlset>\n"
    )
    return Response(content=body, media_type="application/xml; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=3600"})


# --------------------------------------------------------------------------- #
# SPA 壳动态渲染（canonical / og:url / JSON-LD 的绝对地址注入）
# --------------------------------------------------------------------------- #
def _render_index(request: Request) -> HTMLResponse:
    html = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace(config.BASE_URL_TOKEN, html_escape(_base_url(request), quote=True))
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@router.get("/")
def index(request: Request) -> HTMLResponse:
    """首页：接管 StaticFiles 的 html=True 行为，注入站点绝对地址后返回。"""
    return _render_index(request)


@router.get("/index.html")
def index_html(request: Request) -> HTMLResponse:
    """/index.html 与 / 同渲染（防占位符经静态文件原样泄露）。"""
    return _render_index(request)


# --------------------------------------------------------------------------- #
# 品牌化 404（非 /api 路径 + Accept 含 text/html 才走 HTML，其余保持默认 JSON）
# --------------------------------------------------------------------------- #
_404_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta name="robots" content="noindex,follow"/>
<title>页面不存在 - MindPilot | 万能视频下载与AI视频分析工具</title>
<style>
body{margin:0;background:#f5f6f8;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;color:#0f172a}
.wrap{max-width:560px;margin:0 auto;padding:96px 20px;text-align:center}
.code{font-size:64px;font-weight:800;color:#1677ff;line-height:1}
h1{font-size:20px;margin:16px 0 8px}
p{font-size:14px;color:#64748b;margin:0 0 28px}
a{display:inline-block;background:#1677ff;color:#fff;text-decoration:none;border-radius:14px;
padding:12px 28px;font-size:15px;font-weight:700}
</style>
</head>
<body>
<div class="wrap">
  <div class="code">404</div>
  <h1>页面不存在或已被移除</h1>
  <p>链接可能有误，或内容已下线。MindPilot 支持 20+ 平台视频下载与 AI 分析，欢迎回首页体验。</p>
  <a href="/">返回 MindPilot 首页</a>
</div>
</body>
</html>"""


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> Response:
    """覆写默认 HTTPException 处理器：404 分流品牌页，其余语义与 FastAPI 默认一致。"""
    if (exc.status_code == 404
            and not request.url.path.startswith("/api")
            and "text/html" in (request.headers.get("accept") or "")):
        return HTMLResponse(_404_HTML, status_code=404)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                        headers=getattr(exc, "headers", None))
