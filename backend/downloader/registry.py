"""平台解析器注册表。

门面（__init__.py）按 SPECIFIC_EXTRACTORS 顺序调用各模块的 can_handle(url)，
命中即用该专用解析器处理；都不命中则回退到 generic（基于 yt-dlp 的通用解析器）。

新增一个平台的步骤：
1. 新建 backend/downloader/<platform>.py，实现 can_handle / extract_info / download；
2. 在本文件 import 该模块并加入 SPECIFIC_EXTRACTORS（越靠前优先级越高）。
"""

from __future__ import annotations

from backend.downloader import douyin

# 专用解析器列表：每个模块需暴露 can_handle / extract_info / download
SPECIFIC_EXTRACTORS = [
    douyin,
    # bilibili,   # 未来按需新增
    # tiktok,
]
