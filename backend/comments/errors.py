"""评论抓取异常（与转写/总结的异常语义对齐，便于 main.py 统一映射 HTTP 状态）。"""

from __future__ import annotations


class CommentsError(Exception):
    """评论抓取通用失败（网络/解析错误等）。"""


class CommentsNotSupportedError(CommentsError):
    """该平台暂不支持评论抓取，或该视频没有可展示的评论。"""
