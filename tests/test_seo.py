"""SEO 基础设施（seo 包 / 分享落地页 / SPA 壳注入）的离线测试（不触网）。

覆盖：
- robots.txt：屏蔽 /api/、放行全站、按 PUBLIC_BASE_URL 声明 Sitemap
- sitemap.xml：收录首页与未撤销公开分享（含 lastmod），撤销后剔除
- "/" 与 "/index.html"：__SEO_BASE_URL__ 占位符全部替换（canonical / og:url / JSON-LD）
- 分享落地页：meta description / canonical / robots / OG / Twitter / Article JSON-LD；
  失效页（410 撤销 / 404 不存在）带 noindex
- 404 分流：非 /api + Accept text/html → 品牌 HTML 页；/api 路径 → 默认 JSON

运行：python tests/test_seo.py
"""
import os
import sys
import tempfile
from pathlib import Path

# 将项目根目录加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 关键：db.py 在「导入时」即按 DATABASE_URL 建 engine，必须在导入任何 backend.* 之前设置。
_TMP_DIR = Path(tempfile.mkdtemp(prefix="mp_seo_test_"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP_DIR / 'test_seo.db').as_posix()}"
os.environ["PUBLIC_BASE_URL"] = "https://seo.example.com"
os.environ["AUTH_ENABLED"] = "true"
os.environ["AUTH_ALLOW_ANONYMOUS"] = "false"
os.environ["LOGIN_IP_MAX_PER_WINDOW"] = "100000"
os.environ["SHARE_RATE_MAX"] = "100000"   # 落地页/公开 API 有 IP 限流，测试不受干扰
os.environ["PASSWORD_MIN_LENGTH"] = "8"

from fastapi.testclient import TestClient  # noqa: E402

from backend import storage  # noqa: E402
from backend.auth import service as auth_service  # noqa: E402
from backend.main import app  # noqa: E402
from backend.share import store as share_store  # noqa: E402

BASE = "https://seo.example.com"
client = TestClient(app)


def _mk_share(code: str, uid: str, title: str = "测试视频标题") -> dict:
    return share_store.share_create(
        uid, code=code, content_key=f"key_{code}", kind="summary", ref_id="",
        title=title, thumb_url="", excerpt="这是分享摘要内容", payload={"bullets": ["要点一"]},
    )


def test_init_db():
    assert storage.init_db() is True
    print("[db] init_db ok")


def test_robots():
    res = client.get("/robots.txt")
    assert res.status_code == 200
    body = res.text
    assert "User-agent: *" in body
    assert "Disallow: /api/" in body
    assert f"Sitemap: {BASE}/sitemap.xml" in body
    print("[seo] robots.txt ok")


def test_index_injection():
    """/ 与 /index.html：占位符零残留，canonical/og:url/JSON-LD 均为绝对地址。"""
    for path in ("/", "/index.html"):
        res = client.get(path)
        assert res.status_code == 200, path
        html = res.text
        assert "__SEO_BASE_URL__" not in html, f"{path} 占位符残留"
        assert f'rel="canonical" href="{BASE}/"' in html, path
        assert f'property="og:url" content="{BASE}/"' in html, path
        assert "application/ld+json" in html and '"@type": "WebSite"' in html, path
        assert 'name="keywords"' in html and 'name="robots"' in html, path
        assert "<noscript>" in html and "<h1" in html, path   # 无 JS 兜底含唯一 H1
    print("[seo] index.html base-url injection ok")


def test_sitemap():
    uid = auth_service.register("seo-sitemap@example.com", "password123")["id"]
    _mk_share("seositemap01", uid)
    revoked = _mk_share("seositemap02", uid)

    body = client.get("/sitemap.xml").text
    assert f"<loc>{BASE}/</loc>" in body
    assert f"<loc>{BASE}/s/seositemap01</loc>" in body
    assert "<lastmod>" in body and "</lastmod>" in body

    # 撤销后从 sitemap 剔除（sitemap 只收录未撤销分享）
    assert share_store.share_revoke(uid, revoked["code"]) is True
    body = client.get("/sitemap.xml").text
    assert "seositemap02" not in body
    print("[seo] sitemap.xml include/exclude ok")


def test_landing_seo_tags():
    uid = auth_service.register("seo-landing@example.com", "password123")["id"]
    _mk_share("seolanding01", uid, title="深度学习入门课程")

    res = client.get("/s/seolanding01")
    assert res.status_code == 200
    html = res.text
    assert 'name="description"' in html and "深度学习入门课程" in html
    assert f'<link rel="canonical" href="{BASE}/s/seolanding01"/>' in html
    assert '<meta name="robots" content="index,follow"/>' in html
    assert 'property="og:site_name" content="MindPilot"' in html
    assert 'property="og:locale" content="zh_CN"' in html
    assert 'name="twitter:card" content="summary_large_image"' in html
    assert 'name="twitter:title"' in html
    assert "application/ld+json" in html and '"@type": "Article"' in html
    assert '"headline": "深度学习入门课程"' in html
    assert '"datePublished"' in html
    print("[seo] landing page TDK/OG/Twitter/JSON-LD ok")


def test_gone_pages_noindex():
    uid = auth_service.register("seo-gone@example.com", "password123")["id"]
    _mk_share("seogone00001", uid)
    share_store.share_revoke(uid, "seogone00001")

    res = client.get("/s/seogone00001")            # 撤销 → 410 + noindex
    assert res.status_code == 410
    assert "noindex" in res.text

    res = client.get("/s/neverexist1")             # 不存在 → 404 + noindex
    assert res.status_code == 404
    assert "noindex" in res.text
    print("[seo] gone pages noindex ok")


def test_404_split():
    """非 /api + Accept text/html → 品牌 HTML 404；/api 路径 → 默认 JSON 404。"""
    res = client.get("/definitely-missing-page", headers={"accept": "text/html"})
    assert res.status_code == 404
    assert "text/html" in res.headers.get("content-type", "")
    assert "返回 MindPilot 首页" in res.text and "noindex" in res.text

    res = client.get("/api/definitely-missing")
    assert res.status_code == 404
    assert res.json().get("detail") == "Not Found"

    # 静态资源 404（Accept 非 html，如 fetch js）仍走 JSON，不打扰前端错误处理
    res = client.get("/js/missing-module.js")
    assert res.status_code == 404
    assert "text/html" not in res.headers.get("content-type", "")
    print("[seo] 404 html/json split ok")


def _cleanup():
    try:
        from backend.storage import db as sdb
        sdb.engine.dispose()
    except Exception:
        pass
    try:
        import shutil
        shutil.rmtree(_TMP_DIR, ignore_errors=True)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        test_init_db()
        test_robots()
        test_index_injection()
        test_sitemap()
        test_landing_seo_tags()
        test_gone_pages_noindex()
        test_404_split()
        print("SEO TEST PASSED")
    finally:
        _cleanup()
