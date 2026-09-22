"""FastAPI 应用入口。

提供的主要接口：
- GET  /api/info       解析视频链接，返回标题/封面/可选清晰度
- POST /api/download   服务端下载并流式回传文件（手机/网页均可保存）
- POST /api/transcribe 提取视频字幕（转写为带时间戳文本）
- POST /api/summarize  字幕 → 大模型结构化总结（摘要/要点/章节）

同时托管项目根目录 frontend/ 下的单页前端。

启动（在项目根目录执行）：
    python -m backend.main
    # 或：uvicorn backend.main:app --reload --host 0.0.0.0 --port 8010
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

# 兼容两种运行方式：
#   1) 包方式：python -m backend.main / uvicorn backend.main:app
#   2) 脚本方式：python backend/main.py（此时无包上下文，相对导入会失败）
# 脚本方式下把项目根目录注入 sys.path，改用绝对导入即可两种都可用。
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend import ai, downloader, transcribe

# 前端静态目录：项目根目录下的 frontend/
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

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
    """健康检查：告知前端 ffmpeg（影响高清合并）、AI 总结与 ASR 兜底是否可用。"""
    return {
        "status": "ok",
        "ffmpeg": downloader.ffmpeg_available(),
        "ai": ai.ai_available(),
        "ai_provider": ai.current_label(),
        "asr": transcribe.asr_available(),
        "asr_provider": transcribe.asr_label(),
    }


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


@app.post("/api/transcribe")
def post_transcribe(url: str = Body(..., embed=True, min_length=1)) -> JSONResponse:
    """提取视频字幕（转写）。同步 def → 由 Starlette 线程池执行，避免阻塞事件循环。"""
    try:
        data = transcribe.transcribe(url)
    except ValueError as exc:  # TranscribeError 继承自 ValueError
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"转写出错：{exc}") from exc
    return JSONResponse(data)


@app.post("/api/summarize")
def post_summarize(url: str = Body(..., embed=True, min_length=1)) -> JSONResponse:
    """一站式：提取字幕 → 调用大模型生成结构化总结（摘要/要点/章节）。"""
    try:
        tr = transcribe.transcribe(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"转写出错：{exc}") from exc

    try:
        summary = ai.summarize(tr["text"], tr.get("title", ""))
    except ai.AINotConfiguredError as exc:
        # 未配置大模型：503 + 友好提示（前端据此引导配置，而非报“服务器错误”）
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ai.SummarizeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"总结出错：{exc}") from exc

    return JSONResponse({
        "title": tr.get("title"),
        "language": tr.get("language"),
        "source": tr.get("source"),
        "summary": summary,
        "transcript": {
            "char_count": tr.get("char_count"),
            "segment_count": len(tr.get("segments") or []),
        },
    })


# 静态前端（放在最后，避免覆盖 /api 路由）
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static")


if __name__ == "__main__":
    # 支持 `python -m backend.main` 直接启动（需在项目根目录运行）
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8010, reload=False)
