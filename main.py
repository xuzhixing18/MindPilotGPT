"""MindPilot 万能视频下载 —— 启动入口。

运行：
    python main.py
然后浏览器打开 http://127.0.0.1:8000

说明：
- 后端基于 FastAPI + yt-dlp（封装调用，不修改开源项目源码）；
- 前端为 app/static 下的 Tailwind 单页，由 FastAPI 直接托管；
- host=0.0.0.0 便于手机在同一局域网访问（用手机浏览器打开本机 IP:8000）。
"""

import uvicorn


def main() -> None:
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8010,
        reload=False,
    )


if __name__ == "__main__":
    main()
