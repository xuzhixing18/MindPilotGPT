"""阶段0 存储层（内容缓存 / URL 规范化 / 并发去重）的离线测试（不触网、不需真实 Key）。

覆盖：
- keys        ：normalize_url 丢跟踪参/保留 ?v=/host 小写；同视频不同跟踪参得到同一 key；
                summary_key 对 model / prompt_version / text 敏感
- repo        ：put/get_transcript、put/get_summary 往返一致；转写 TTL 过期视为未命中；
                CACHE_ENABLED=false 时读写短路
- singleflight：两线程并发同 key，底层 compute 只执行一次
- 门面缓存     ：monkeypatch subtitles.transcribe 后，第二次 transcribe(url) 命中缓存
                （cached=True 且不再调用底层）；refresh=True 强制重算

运行：python tests/test_storage_cache.py
"""
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 将项目根目录加入 sys.path，保证从任意位置运行都能导入 backend 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 关键：db.py 在「导入时」即按 DATABASE_URL 创建 engine，故必须在导入任何 backend.*
# 之前，把 DATABASE_URL 指向一个临时库，避免污染项目内的 data/mindpilot.db。
_TMP_DIR = Path(tempfile.mkdtemp(prefix="mp_cache_test_"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP_DIR / 'test_cache.db').as_posix()}"
os.environ["CACHE_ENABLED"] = "true"
os.environ.pop("TRANSCRIPT_CACHE_DAYS", None)

from backend import storage  # noqa: E402
from backend.storage import db as sdb  # noqa: E402
from backend.storage import keys, models, repo, singleflight  # noqa: E402


def _transcript_stub(text: str = "你好世界") -> dict:
    """构造一份与 subtitles/asr 转写结果同构的样例数据。"""
    return {
        "title": "测试视频",
        "language": "zh",
        "language_name": "语音识别 · 硅基流动",
        "source": "asr",
        "segments": [{"start": 0.0, "end": 2.0, "text": text}],
        "text": text,
        "char_count": len(text),
        "webpage_url": "https://www.bilibili.com/video/BV1test",
        "asr_provider": "硅基流动 SenseVoice",
    }


def test_init_db():
    assert storage.init_db() is True
    print("[db] init_db ok")


def test_normalize_url():
    # 丢弃 utm_* / spm 等跟踪参数，保留真正标识视频的 ?v=
    a = keys.normalize_url("https://www.youtube.com/watch?v=abc123&utm_source=x&spm=y")
    assert "youtube.com" in a and "YouTube" not in a  # host 小写化
    assert "v=abc123" in a
    assert "utm_source" not in a and "spm" not in a

    # 去 fragment
    assert "#" not in keys.normalize_url("https://x.com/p?a=1#frag")

    # 同一视频不同跟踪参 -> 规范化后一致 -> 同一 key
    k1 = keys.transcript_key("https://www.bilibili.com/video/BV1xx?spm_id_from=333.337&vd_source=abc")
    k2 = keys.transcript_key("https://www.bilibili.com/video/BV1xx")
    assert k1 == k2

    # 不同视频 -> 不同 key
    assert keys.transcript_key("https://www.bilibili.com/video/BV1aa") != k2
    print("[keys] normalize_url / transcript_key ok")


def test_summary_key_sensitive():
    base = keys.summary_key("text", "modelA", "v1")
    assert base == keys.summary_key("text", "modelA", "v1")  # 稳定可复现
    variants = {
        base,
        keys.summary_key("text", "modelB", "v1"),   # 换模型
        keys.summary_key("text", "modelA", "v2"),   # 换提示词版本
        keys.summary_key("text2", "modelA", "v1"),  # 换文本
    }
    assert len(variants) == 4  # 任一维度变化即不同 key
    print("[keys] summary_key sensitivity ok")


def test_repo_transcript_roundtrip():
    url = "https://www.bilibili.com/video/BV1roundtrip"
    key = keys.transcript_key(url)
    repo.put_transcript(key, _transcript_stub("往返一致"), url, keys.normalize_url(url))
    got = repo.get_transcript(key)
    assert got is not None
    assert got["title"] == "测试视频"
    assert got["text"] == "往返一致"
    assert got["source"] == "asr"
    assert got["segments"] == [{"start": 0.0, "end": 2.0, "text": "往返一致"}]
    assert got["asr_provider"] == "硅基流动 SenseVoice"
    assert "cached" not in got  # 存储不含运行时标记
    print("[repo] transcript roundtrip ok")


def test_repo_summary_roundtrip():
    key = keys.summary_key("总结文本", "deepseek · deepseek-chat", "v1")
    summary = {
        "one_line": "一句话", "summary": "摘要", "key_points": ["a", "b"],
        "chapters": [{"title": "c", "summary": "s"}], "keywords": ["k"],
        "model": "deepseek · deepseek-chat", "truncated": False, "cached": False,
    }
    repo.put_summary(key, summary, model="deepseek · deepseek-chat", prompt_version="v1", title="标题")
    got = repo.get_summary(key)
    assert got is not None
    assert got["one_line"] == "一句话"
    assert got["key_points"] == ["a", "b"]
    assert got["chapters"] == [{"title": "c", "summary": "s"}]
    assert "cached" not in got  # put 时已剔除运行时 cached 标记
    print("[repo] summary roundtrip ok")


def test_repo_transcript_expiry():
    url = "https://youtube.com/watch?v=expireme"
    key = keys.transcript_key(url)
    repo.put_transcript(key, _transcript_stub("会过期"), url, keys.normalize_url(url))
    assert repo.get_transcript(key) is not None  # 新鲜时命中

    # 手动把 created_at 拨到远超 TTL（默认 30 天）之前 -> 视为过期未命中
    with storage.session() as s:
        row = s.get(models.Transcript, key)
        row.created_at = datetime.now(timezone.utc) - timedelta(days=999)
    assert repo.get_transcript(key) is None

    # TRANSCRIPT_CACHE_DAYS<=0 表示永不过期：同样陈旧的行此时应命中
    os.environ["TRANSCRIPT_CACHE_DAYS"] = "0"
    try:
        assert repo.get_transcript(key) is not None
    finally:
        os.environ.pop("TRANSCRIPT_CACHE_DAYS", None)
    print("[repo] transcript TTL expiry ok")


def test_cache_disabled_shortcircuit():
    url = "https://x.com/disabled"
    key = keys.transcript_key(url)
    os.environ["CACHE_ENABLED"] = "false"
    try:
        repo.put_transcript(key, _transcript_stub("不该被写入"), url, keys.normalize_url(url))
        assert repo.get_transcript(key) is None  # 关闭时写跳过、读未命中
        skey = keys.summary_key("t", "m", "v1")
        repo.put_summary(skey, {"one_line": "x"}, model="m", prompt_version="v1")
        assert repo.get_summary(skey) is None
    finally:
        os.environ["CACHE_ENABLED"] = "true"
    print("[repo] CACHE_ENABLED=false short-circuit ok")


def test_singleflight_dedup():
    sf = singleflight.SingleFlight()
    calls = {"n": 0}
    holder: dict = {}
    barrier = threading.Barrier(2)

    def fn():
        # 模拟调用方的「双重检查」：拿到锁后先看是否已算过
        if "v" in holder:
            return holder["v"]
        calls["n"] += 1
        time.sleep(0.05)  # 放大竞态窗口
        holder["v"] = "computed"
        return holder["v"]

    def worker():
        barrier.wait()  # 两线程尽量同时进入
        sf.run("same-key", fn)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls["n"] == 1, f"single-flight 应只计算一次，实际 {calls['n']}"
    print("[singleflight] concurrent same-key computed once ok")


def test_facade_cache():
    from backend import transcribe as tc
    from backend.transcribe import subtitles

    calls = {"n": 0}

    def fake_transcribe(url):
        calls["n"] += 1
        return {
            "title": "假视频", "language": "zh", "language_name": "自动字幕",
            "source": "auto", "segments": [{"start": 0.0, "end": 1.0, "text": "嗨"}],
            "text": "嗨", "char_count": 1, "webpage_url": url,
        }

    url = "https://www.bilibili.com/video/BV1facade"
    orig = subtitles.transcribe
    subtitles.transcribe = fake_transcribe
    try:
        r1 = tc.transcribe(url)
        assert r1["cached"] is False and calls["n"] == 1  # 首次未命中，真正计算

        r2 = tc.transcribe(url)
        assert r2["cached"] is True and calls["n"] == 1   # 第二次命中缓存，不再计算
        assert r2["text"] == "嗨"

        r3 = tc.transcribe(url, refresh=True)
        assert r3["cached"] is False and calls["n"] == 2  # refresh 强制重算并覆盖

        r4 = tc.transcribe(url)
        assert r4["cached"] is True and calls["n"] == 2   # 重算后再次命中
    finally:
        subtitles.transcribe = orig
    print("[facade] transcribe cache hit / refresh ok")


def _cleanup():
    """尽力释放引擎并删除临时库（Windows 下 WAL 文件可能短暂占用，失败即忽略）。"""
    try:
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
        test_normalize_url()
        test_summary_key_sensitive()
        test_repo_transcript_roundtrip()
        test_repo_summary_roundtrip()
        test_repo_transcript_expiry()
        test_cache_disabled_shortcircuit()
        test_singleflight_dedup()
        test_facade_cache()
        print("STORAGE CACHE TEST PASSED")
    finally:
        _cleanup()
