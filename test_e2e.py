"""端到端测试：真实解析 + 下载一个 B站视频，验证整条链路。"""
import json
import time
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8000"
VIDEO = "https://www.bilibili.com/video/BV1GJ411x7h7"


def get(path, timeout=120):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as resp:
        return resp.read(), resp.headers, resp.status


def main():
    # 1) health
    body, _, _ = get("/api/health")
    print("[health]", body.decode())

    # 2) info
    q = urllib.parse.urlencode({"url": VIDEO})
    body, _, _ = get("/api/info?" + q, timeout=120)
    info = json.loads(body.decode("utf-8"))
    print("[info] title:", info["title"])
    print("[info] ffmpeg_available:", info["ffmpeg_available"])
    print("[info] formats:")
    video_fmt = None
    for f in info["formats"]:
        print("   -", f["label"], "| id=", f["format_id"], "| progressive=", f["progressive"])
        # 选一个「含画面」的清晰度用于下载测试（优先 progressive，避免耗时的合并）
        if not f.get("is_audio_only") and video_fmt is None:
            video_fmt = f["format_id"]

    # 3) download（选一个视频清晰度）
    assert video_fmt, "没有找到可下载的视频清晰度"
    q = urllib.parse.urlencode({"url": VIDEO, "format_id": video_fmt})
    t0 = time.time()
    body, headers, status = get("/api/download?" + q, timeout=300)
    dt = time.time() - t0
    out = "e2e_download.bin"
    with open(out, "wb") as fh:
        fh.write(body)
    print(f"[download] format_id={video_fmt} status={status} bytes={len(body)} time={dt:.1f}s")
    print("[download] content-disposition:", headers.get("content-disposition"))
    print("E2E TEST PASSED")


if __name__ == "__main__":
    main()
