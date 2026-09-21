"""FastAPI 应用入口。

提供两类接口：
- GET  /api/info     解析视频链接，返回标题/封面/可选清晰度
- POST /api/download 服务端下载并流式回传文件（手机/网页均可保存）

同时托管 app/static 下的单页前端（模仿 BibiGPT 风格）。

启动：
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from app import downloader

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="MindPilot 视频下载", version="0.1.0")

# 允许跨域，便于前端分离部署或本地调试
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _cleanup(path_str: str) -> None:
    """回传完成后删除服务端临时文件。"""
    try:
        Path(path_str).unlink(missing_ok=True)
    except OSError:
        pass


@app.get("/api/health")
def health() -> dict:
    """健康检查，同时告知前端 ffmpeg 是否可用（影响高清合并）。"""
    return {"status": "ok", "ffmpeg": downloader.ffmpeg_available()}


@app.get("/api/info")
def get_info(url: str = Query(..., min_length=1, description="视频链接")) -> JSONResponse:
    """解析视频信息。同步 def → 由 Starlette 线程池执行，避免阻塞事件循环。"""
    try:
        data = downloader.extract_info(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 兜底，避免把堆栈直接暴露给前端
        raise HTTPException(status_code=500, detail=f"解析出错：{exc}") from exc
    return JSONResponse(data)


@app.post("/api/download")
def post_download(
    url: str = Body(..., embed=True, min_length=1),
    format_id: str | None = Body(None, embed=True),
) -> FileResponse:
    """服务端下载并流式回传文件（POST/JSON 形式）。"""
    return _download_response(url, format_id)


@app.get("/api/download")
def get_download(
    url: str = Query(..., min_length=1),
    format_id: str | None = Query(None),
) -> FileResponse:
    """服务端下载并流式回传文件（GET 形式）。

    用 GET 便于前端直接通过 <a href> / window.location 触发浏览器
    原生下载，对大文件与手机端更友好（无需把文件读进内存）。
    """
    return _download_response(url, format_id)


def _download_response(url: str, format_id: str | None) -> FileResponse:
    """下载并构造 FileResponse 的公共逻辑。"""
    try:
        result = downloader.download(url, format_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"下载出错：{exc}") from exc

    return FileResponse(
        path=result["filepath"],
        filename=result["filename"],
        media_type="application/octet-stream",
        background=BackgroundTask(_cleanup, result["filepath"]),
    )


# 静态前端（放在最后，避免覆盖 /api 路由）
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
