"""在线流式播放 /api/stream 的离线测试（不触网：stub 掉 downloader.download）。

覆盖：
- _range_response：全量 200 / 区间 206 + Content-Range / 后缀 bytes=-N / 越界 416
- HTTP（TestClient）：首次请求触发下载并落缓存、Range 分块、二次请求命中缓存
  不重复下载、下载失败映射 400

运行：python tests/test_stream.py
"""
import os
import sys
import tempfile
from pathlib import Path

# 将项目根目录加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 关键：db.py 在「导入时」即按 DATABASE_URL 建 engine，必须在导入任何 backend.* 之前设置。
_TMP_DIR = Path(tempfile.mkdtemp(prefix="mp_stream_test_"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP_DIR / 'test_stream.db').as_posix()}"
os.environ["AUTH_ENABLED"] = "false"   # 关闭门禁，聚焦流播逻辑本身

from backend import main as main_mod  # noqa: E402

_PAYLOAD = bytes(range(100))   # 100 字节假视频（0..99）


def _mk_file(dirpath: Path, name: str = "fake.mp4") -> Path:
    p = dirpath / name
    p.write_bytes(_PAYLOAD)
    return p


# --------------------------------------------------------------------------- #
# _range_response 单元级
# --------------------------------------------------------------------------- #
def test_range_response_units():
    src = _mk_file(_TMP_DIR)

    # 无 Range → 200 全量
    resp = main_mod._range_response(src, None)
    assert resp.status_code == 200
    assert resp.headers["Accept-Ranges"] == "bytes"
    assert resp.headers["Content-Length"] == "100"

    # 区间 → 206 + Content-Range
    resp = main_mod._range_response(src, "bytes=10-19")
    assert resp.status_code == 206
    assert resp.headers["Content-Range"] == "bytes 10-19/100"
    assert resp.headers["Content-Length"] == "10"

    # 开放结尾 bytes=95- → 取到文件末尾
    resp = main_mod._range_response(src, "bytes=95-")
    assert resp.status_code == 206
    assert resp.headers["Content-Range"] == "bytes 95-99/100"

    # 后缀形式 bytes=-8 → 末尾 8 字节
    resp = main_mod._range_response(src, "bytes=-8")
    assert resp.status_code == 206
    assert resp.headers["Content-Range"] == "bytes 92-99/100"

    # end 越界 → 截断到 size-1；start 越界 → 416
    resp = main_mod._range_response(src, "bytes=50-9999")
    assert resp.headers["Content-Range"] == "bytes 50-99/100"
    resp = main_mod._range_response(src, "bytes=100-")
    assert resp.status_code == 416
    print("[unit] _range_response 200/206/suffix/416 ok")


# --------------------------------------------------------------------------- #
# HTTP（TestClient）
# --------------------------------------------------------------------------- #
def test_http_stream_flow():
    from fastapi.testclient import TestClient

    from backend.main import app

    # 缓存目录与映射指向本测试的临时空间
    stream_dir = _TMP_DIR / "stream_cache"
    orig_dir, orig_cache = main_mod._STREAM_DIR, main_mod._stream_cache
    main_mod._STREAM_DIR = stream_dir
    main_mod._stream_cache = {}

    calls = {"n": 0}

    def fake_download(url, format_id=None):
        calls["n"] += 1
        if "bad" in url:
            raise ValueError("不支持的链接")
        staging = _TMP_DIR / "staging"
        staging.mkdir(exist_ok=True)
        return {"filepath": str(_mk_file(staging, f"dl_{calls['n']}.mp4")), "filename": "fake.mp4"}

    orig_download = main_mod.downloader.download
    main_mod.downloader.download = fake_download
    try:
        with TestClient(app) as client:
            url = "https://v.douyin.com/stream0001"

            # 首次：触发下载 → 200 全量
            r = client.get("/api/stream", params={"url": url})
            assert r.status_code == 200, r.text
            assert r.content == _PAYLOAD
            assert r.headers["Accept-Ranges"] == "bytes"
            assert calls["n"] == 1

            # Range：206 分块（video 标签 seek 依赖）
            r = client.get("/api/stream", params={"url": url}, headers={"Range": "bytes=10-19"})
            assert r.status_code == 206
            assert r.headers["Content-Range"] == "bytes 10-19/100"
            assert r.content == bytes(range(10, 20))

            # 二次全量：命中磁盘缓存，不再触发下载
            r = client.get("/api/stream", params={"url": url})
            assert r.status_code == 200 and calls["n"] == 1

            # 缓存文件落在流播目录且被内存映射记录
            files = list(stream_dir.glob("*.mp4"))
            assert len(files) == 1 and files[0].read_bytes() == _PAYLOAD
            assert len(main_mod._stream_cache) == 1

            # 下载失败（ValueError）→ 400
            r = client.get("/api/stream", params={"url": "https://v.douyin.com/bad-link"})
            assert r.status_code == 400
    finally:
        main_mod.downloader.download = orig_download
        main_mod._STREAM_DIR, main_mod._stream_cache = orig_dir, orig_cache
    print("[http] stream download/cache/range/400 flow ok")


def _cleanup():
    try:
        from backend.storage import db as sdb
        sdb.engine.dispose()
    except Exception:
        pass
    import shutil
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


if __name__ == "__main__":
    try:
        test_range_response_units()
        test_http_stream_flow()
        print("STREAM TEST PASSED")
    finally:
        _cleanup()
