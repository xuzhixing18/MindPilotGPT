"""GEO（生成式引擎优化）改动的离线测试（不触网，规范见 docs/GEO优化入门指南.md）。

覆盖：
- robots.txt：AI 搜索/引用类爬虫永远显式放行；训练类按 GEO_ALLOW_TRAIN_BOTS 开关整组 Disallow
- llms.txt：纯 Markdown 站点自述（llmstxt.org 约定），含公开内容页与 sitemap 指引
- 落地页：结论先行（一句话结论 h2 首块）、heading 语义化（非 bundle h2 / bundle h2>h3 嵌套）、
  章节速览（带/不带时间戳）、生成日期新鲜度标记
- FAQPage JSON-LD：qa 载体与 bundle qa 段生成且与可见「问答节选」一致；无问答载体不生成
- 快照消毒：chapters 白名单（title 必填截断、start 非法归 None、非 dict 丢弃、条数上限）
- 首页：FAQPage JSON-LD 与 noscript 兜底 FAQ 同在

运行：python tests/test_geo.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# db.py 在「导入时」即按 DATABASE_URL 建 engine，必须在导入任何 backend.* 之前设置。
_TMP_DIR = Path(tempfile.mkdtemp(prefix="mp_geo_test_"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP_DIR / 'test_geo.db').as_posix()}"
os.environ["PUBLIC_BASE_URL"] = "https://geo.example.com"
os.environ["AUTH_ENABLED"] = "true"
os.environ["AUTH_ALLOW_ANONYMOUS"] = "false"
os.environ["LOGIN_IP_MAX_PER_WINDOW"] = "100000"
os.environ["SHARE_RATE_MAX"] = "100000"
os.environ["PASSWORD_MIN_LENGTH"] = "8"
os.environ.pop("GEO_ALLOW_TRAIN_BOTS", None)   # 默认态：训练类放行

from fastapi.testclient import TestClient  # noqa: E402

from backend import storage  # noqa: E402
from backend.auth import service as auth_service  # noqa: E402
from backend.main import app  # noqa: E402
from backend.share import store as share_store  # noqa: E402
from backend.share.service import _clean_snapshot  # noqa: E402

BASE = "https://geo.example.com"
client = TestClient(app)


def _mk(code: str, uid: str, kind: str = "summary", excerpt: str = "一句话结论内容",
        payload: dict | None = None, title: str = "GEO 测试视频") -> dict:
    return share_store.share_create(
        uid, code=code, content_key=f"key_{code}", kind=kind, ref_id="",
        title=title, thumb_url="", excerpt=excerpt,
        payload={"normalized_url": "", **(payload or {})},
    )


def test_init_db():
    assert storage.init_db() is True
    print("[db] init_db ok")


def test_robots_search_bots_always_allowed():
    """AI 搜索/引用类爬虫显式放行；默认不输出训练类屏蔽组。"""
    body = client.get("/robots.txt").text
    for bot in ("OAI-SearchBot", "ChatGPT-User", "PerplexityBot", "Claude-User"):
        assert f"User-agent: {bot}\nAllow: /" in body, bot
    assert "User-agent: GPTBot" not in body          # 默认放行：不出现屏蔽组
    assert "User-agent: *\nAllow: /\nDisallow: /api/" in body   # 通配组语义不变
    assert f"Sitemap: {BASE}/sitemap.xml" in body
    print("[geo] robots search bots allowed ok")


def test_robots_train_bots_switch():
    """GEO_ALLOW_TRAIN_BOTS=false → 训练类爬虫整组 Disallow。"""
    os.environ["GEO_ALLOW_TRAIN_BOTS"] = "false"
    try:
        body = client.get("/robots.txt").text
        assert "User-agent: GPTBot\nUser-agent: Google-Extended" in body
        assert "\nDisallow: /\n" in body
        # 搜索类不受开关影响，仍显式放行
        assert "User-agent: PerplexityBot\nAllow: /" in body
    finally:
        os.environ.pop("GEO_ALLOW_TRAIN_BOTS", None)
    print("[geo] robots train bots switch ok")


def test_llms_txt():
    res = client.get("/llms.txt")
    assert res.status_code == 200
    assert "text/markdown" in res.headers.get("content-type", "")
    body = res.text
    assert body.startswith("# MindPilot")
    assert f"{BASE}/s/" in body and f"{BASE}/sitemap.xml" in body
    assert "yt-dlp" in body or "视频解析下载" in body
    print("[geo] llms.txt ok")


def test_landing_conclusion_first_and_headings():
    """结论先行 + heading 语义化 + 章节速览（带/不带时间戳）+ 新鲜度标记。"""
    uid = auth_service.register("geo-landing@example.com", "password123")["id"]
    _mk("geoland00001", uid, excerpt="AI 生成的一句话结论", payload={
        "bullets": ["要点一", "要点二"],
        "chapters": [{"title": "开场介绍", "start": 0},
                     {"title": "核心方法", "start": 193},
                     {"title": "无时间戳章节", "start": None}],
    })
    html = client.get("/s/geoland00001").text
    assert '<h2 class="sec">一句话结论</h2>' in html
    assert '<p class="excerpt">AI 生成的一句话结论</p>' in html
    assert '<h2 class="sec">关键要点</h2>' in html and "<li>要点一</li>" in html
    assert '<h2 class="sec">章节速览</h2>' in html
    assert '<span class="ch-ts">00:00</span>开场介绍' in html
    assert '<span class="ch-ts">03:13</span>核心方法' in html
    assert "<li>无时间戳章节</li>" in html and "ch-ts\">无" not in html
    assert "生成于 " in html                        # 新鲜度标记（created_at 日期）
    assert '<div class="sec">' not in html           # 旧 div 标题零残留
    print("[geo] landing conclusion/headings/chapters ok")


def test_landing_bundle_heading_nesting():
    """bundle：分组标题 h2.sec-h，组内小节降为 h3（h1>h2>h3 有序嵌套）。"""
    uid = auth_service.register("geo-bundle@example.com", "password123")["id"]
    _mk("geobundle01", uid, kind="bundle", excerpt="合辑摘要", payload={
        "kinds": ["summary", "qa"],
        "sections": {
            "summary": {"excerpt": "合辑内一句话结论", "bullets": ["合辑要点"]},
            "qa": {"excerpt": "问题一", "qa": [{"q": "问题一", "a": "答案一"}]},
        },
    })
    html = client.get("/s/geobundle01").text
    assert '<h2 class="sec-h">AI 总结</h2>' in html
    assert '<h2 class="sec-h">AI 问答</h2>' in html
    assert '<h3 class="sec">一句话结论</h3>' in html
    assert '<h3 class="sec">问答节选</h3>' in html
    print("[geo] bundle heading nesting ok")


def test_faq_jsonld():
    """qa 载体/bundle qa 段生成 FAQPage，且与可见问答节选同一数据；summary 不生成。"""
    uid = auth_service.register("geo-faq@example.com", "password123")["id"]
    _mk("geofaq00001", uid, kind="qa", excerpt="什么是 GEO？",
        payload={"qa": [{"q": "什么是 GEO？", "a": "让内容被 AI 引擎引用与推荐。"}]})
    html = client.get("/s/geofaq00001").text
    assert '"@type": "FAQPage"' in html
    assert '"name": "什么是 GEO？"' in html
    assert '"text": "让内容被 AI 引擎引用与推荐。"' in html
    assert "问：什么是 GEO？" in html                # 可见正文与 JSON-LD 同源
    assert '"@type": "Article"' in html              # Article 仍并存

    _mk("geofaq00002", uid, kind="bundle", excerpt="", payload={
        "kinds": ["qa"],
        "sections": {"qa": {"excerpt": "Q2", "qa": [{"q": "Q2？", "a": "A2。"}]}},
    })
    assert '"@type": "FAQPage"' in client.get("/s/geofaq00002").text

    _mk("geofaq00003", uid, excerpt="纯总结无问答", payload={"bullets": ["b"]})
    html = client.get("/s/geofaq00003").text
    assert "FAQPage" not in html                     # 无问答不生成，不占位
    print("[geo] FAQPage jsonld ok")


def test_chapters_sanitize():
    """快照消毒白名单：title 必填截断、start 非法归 None、非 dict 丢弃、条数上限。

    截断语义与既有 comments 消毒同范式：先取前 CHAPTERS_MAX 条再逐条过滤。
    """
    snap = {"chapters": [
        {"title": "正常章节", "start": 61.7},
        {"title": "负数时间", "start": -5},
        {"title": "非法类型", "start": "abc"},
        {"title": "超一天", "start": 999999},
        {"title": "", "start": 1},                   # 空 title 丢弃
        "不是字典",                                    # 非 dict 丢弃
        {"title": "x" * 100, "start": None},          # 截断到 CH_TITLE_MAX
    ]}
    _, payload = _clean_snapshot("summary", snap, "norm")
    chs = payload["chapters"]
    assert len(chs) == 5                              # 7 条入、2 条废，剩 5 条有效
    assert chs[0] == {"title": "正常章节", "start": 61.7}
    assert chs[1]["start"] is None and chs[2]["start"] is None and chs[3]["start"] is None
    assert all(c["title"] for c in chs)
    assert len(chs[4]["title"]) == 60                 # CH_TITLE_MAX 截断

    _, payload = _clean_snapshot(
        "summary", {"chapters": [{"title": f"填充{i}"} for i in range(20)]}, "norm")
    assert len(payload["chapters"]) == 10             # CHAPTERS_MAX 上限
    print("[geo] chapters sanitize ok")


def test_index_faq_and_noscript():
    """首页：FAQPage JSON-LD 与 noscript 兜底 FAQ 同时在场（结构化数据=可见内容）。"""
    html = client.get("/").text
    assert '"@type": "FAQPage"' in html
    assert "MindPilot 是什么？" in html
    assert "支持哪些视频平台？" in html
    assert "<noscript>" in html and "常见问题" in html
    print("[geo] index FAQPage + noscript ok")


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
        test_robots_search_bots_always_allowed()
        test_robots_train_bots_switch()
        test_llms_txt()
        test_landing_conclusion_first_and_headings()
        test_landing_bundle_heading_nesting()
        test_faq_jsonld()
        test_chapters_sanitize()
        test_index_faq_and_noscript()
        print("GEO TEST PASSED")
    finally:
        _cleanup()
