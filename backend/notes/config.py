"""notes 配置：笔记 / 图片的上限与截断（env 驱动，与 library/config.py 同范式）。

读取函数每次调用读 env（测试可运行时改 env 生效），解析失败回退默认值。
"""

from __future__ import annotations

import os
from pathlib import Path

# 项目根目录（backend/notes/config.py → notes → backend → root）
_ROOT = Path(__file__).resolve().parents[2]

# 笔记图片默认存储目录（可经 NOTE_IMAGE_DIR 覆盖，与 AVATAR_DIR 同一解析规则）
DEFAULT_NOTE_IMAGE_DIR = _ROOT / "data" / "note_images"

# 时间戳标记在 Markdown 源文中的格式：@[03:12](t=192)（人类可读、导出天然带时间、正则易解析）
# 服务端 marks 解析与前端 chip 渲染共用该格式，是前后端的单一约定。
TS_MARK_RE = r"@\[(\d{1,2}:\d{2}(?::\d{2})?)\]\(t=(\d+(?:\.\d+)?)\)"


def _int(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or str(default)).strip())
    except ValueError:
        return default


def note_max_chars() -> int:
    """单篇笔记正文字符上限（默认 20000；服务端截断不报错，返回 truncated 供前端提示）。"""
    return _int("NOTE_MAX_CHARS", 20_000)


def notes_max_per_user() -> int:
    """每用户笔记数上限，超限滚动淘汰最旧不活跃（默认 500，与历史同量级）。"""
    return _int("NOTES_MAX_PER_USER", 500)


def note_images_max_per_note() -> int:
    """单篇笔记图片数上限（默认 30）。"""
    return _int("NOTE_IMAGES_MAX_PER_NOTE", 30)


def note_image_max_bytes() -> int:
    """单张图片体积上限（默认 5MB；截图 PNG 通常 300KB~1.5MB）。"""
    return _int("NOTE_IMAGE_MAX_BYTES", 5 * 1024 * 1024)


def note_image_total_max_bytes() -> int:
    """每用户图片总量上限（默认 500MB；超限惰性淘汰最旧孤儿）。"""
    return _int("NOTE_IMAGE_TOTAL_MAX_BYTES", 500 * 1024 * 1024)


def note_image_dir() -> Path:
    """笔记图片落盘目录：相对路径按项目根解析，绝对路径直接用。"""
    raw = (os.getenv("NOTE_IMAGE_DIR") or "").strip()
    if not raw:
        return DEFAULT_NOTE_IMAGE_DIR
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (_ROOT / p).resolve()
