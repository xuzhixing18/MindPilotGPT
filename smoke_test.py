"""冒烟测试：验证导入与关键逻辑（不依赖网络）。"""
from app.main import app
from app import downloader

print("import ok")
print("routes:", [r.path for r in app.routes if hasattr(r, "path")])
print("ffmpeg_available:", downloader.ffmpeg_available())

# 测试清晰度整理逻辑（用构造的假 info，不走网络）
fake_info = {
    "formats": [
        {"format_id": "18", "ext": "mp4", "height": 360, "vcodec": "avc1", "acodec": "mp4a", "filesize": 5_000_000},
        {"format_id": "22", "ext": "mp4", "height": 720, "vcodec": "avc1", "acodec": "mp4a", "filesize": 12_000_000},
        {"format_id": "137", "ext": "mp4", "height": 1080, "vcodec": "avc1", "acodec": "none", "filesize": 30_000_000},
        {"format_id": "140", "ext": "m4a", "height": None, "vcodec": "none", "acodec": "mp4a", "abr": 128, "filesize": 4_000_000},
    ]
}
fmts = downloader._collect_formats(fake_info)
print("collected formats:")
for f in fmts:
    print("  ", f["label"], "| progressive=", f["progressive"], "| id=", f["format_id"])
audio = downloader._collect_audio_option(fake_info)
print("audio option:", audio["label"] if audio else None)
print("SMOKE TEST PASSED")
