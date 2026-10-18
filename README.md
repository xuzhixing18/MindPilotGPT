# MindPilot — 万能视频解析下载 + AI 内容加工

> 粘贴链接 → 解析 → 下载 / 在线播放 / 字幕转写 / AI 总结 / 思维导图 / 高赞评论 / 多轮问答 / 随手笔记；
> 登录用户自动沉淀处理历史、合集与 QA 会话，并可把分析结果以短链分享出去。
> 技术栈：FastAPI + SQLAlchemy + yt-dlp（后端）· 原生 ESM 单页 + Tailwind（前端，零构建零依赖）。

## 功能一览

- **视频解析下载**：抖音（专用去水印链路）/ 快手 / B 站 / YouTube 等 yt-dlp 覆盖站，清晰度选择、仅音频、ffmpeg 合并；微信视频号给出明确指引拦截
- **在线播放**：`/api/stream` 服务端流式代理（磁盘缓存 + Range），无外链播放器的平台也能看
- **字幕转写**：平台内嵌字幕优先 → 语音识别兜底（Fun-ASR 带时间戳 → 硅基免费 → Qwen3-ASR，链式降级）
- **AI 加工**：结构化总结 / 思维导图（含章节时间线，导出 PNG/SVG/Markdown/XMind）/ 基于字幕的多轮问答
- **高赞评论**：B 站 / 抖音 / 快手 / 喜马拉雅专用抓取 + 通用兜底（全程无 LLM）
- **知识沉淀**：处理历史 / 合集 / QA 会话跨设备续聊 / 随手笔记（时间戳跳播、截图、自动保存）
- **分享与增长**：短链落地页（SSR，SEO 友好）+ 分享海报 + 多渠道分发
- **模型选择**：模型目录已 DB 化，管理端在线上下架，用户可选默认模型

## 快速开始

```powershell
# 1. 安装依赖（Python 3.11+）
pip install -r requirements.txt

# 2. 配置：复制 .env.example 为 .env 按需填写（不配任何 Key 也能用下载/字幕提取）
copy .env.example .env

# 3. 启动（默认 http://0.0.0.0:8010，前端由后端 StaticFiles 托管）
python -m backend.main
# 或：uvicorn backend.main:app --reload --host 0.0.0.0 --port 8010
```

- 健康检查：`GET /api/health`（ffmpeg / AI / ASR / DB / 鉴权状态）
- 测试：`tests/` 下 22 个离线测试文件，逐文件独立进程运行，如 `.venv\Scripts\python.exe tests\test_library.py`
- 最小可用配置（全部可选）：`DEEPSEEK_API_KEY`（AI 总结）、`SILICONFLOW_API_KEY`（免费 ASR）、
  `DASHSCOPE_API_KEY`（带时间戳转写）、`COOKIES_FROM_BROWSER=chrome`（B 站语音字幕）、
  `PUBLIC_BASE_URL`（分享域名，部署必配）、`ADMIN_API_TOKEN`（模型目录管理端点）
- 安全红线：真实 Key 只放 `.env`（已 gitignore），`.env.example` 永远保持占位符

## 架构速览

```
backend/（10 个能力包，门面 + 可插拔）
├── main.py      入口：业务端点 + 三态鉴权门禁 + /api/stream + 管理端点 + 静态托管
├── storage/     底座：15 张 ORM 表 + 缓存键 + TTL + single-flight + 声明式迁移
├── downloader/  解析下载：registry（视频号拦截/抖音/快手）→ generic(yt-dlp) 兜底
├── transcribe/  转写两级：字幕优先 → ASR 链（Fun-ASR→硅基→Qwen3-ASR）兜底
├── ai/          总结/导图/问答 + JSON 三级修复 + 模型目录 DB（多服务商可切换）
├── comments/    高赞评论：bilibili/douyin/kuaishou/ximalaya → generic
├── auth/        注册/登录/会话/资料/头像/默认模型（邮箱+手机号双通道）
├── library/     历史/合集/QA 会话（/api/me/*，user_id 列级隔离）
├── notes/       随手笔记：正文/图片/抽帧 + 自动保存冲突保护
├── share/       分享：短链 + SSR 落地页 + 海报数据 + QR + 限流
└── seo/         robots.txt / llms.txt / sitemap.xml + 品牌化 404

frontend/（原生 ESM 单页，hash 路由多视图，14 个 JS 模块）
player.js      仓库根目录的桥接文件：前端加载它挂载三形态播放器（B站 iframe / YouTube / 原生流播），
               后端提供对应的 /api/stream 媒体流——是本仓库唯一横跨前后端的顶层文件
```

贯穿全项目的原则：**门面 + 可插拔**（新增平台/服务商只改注册表一行）、**LLM 边界纪律**（确定性 IO 绝不调 LLM）、
**缓存优先 + single-flight**（昂贵结果全局共享、二次秒开）、**密钥只来自环境**、**前端零构建零依赖**。

## 文档

完整文档在 [`docs/`](docs/) 目录，入口为 [docs/README.md](docs/README.md)（文档导航：按角色/功能/场景的阅读路径 + 全部文档索引）。

> 分工约定：**本 README 只做门面与跳转，不做深入解释**——功能细节、API 清单、数据模型、配置项、问题清单
> 均以 docs/ 为准（实现总结 → 全景分析报告 → 各功能设计方案），避免两处内容漂移。

## 目录结构

```
backend/   # FastAPI 后端（10 个能力包）
frontend/  # 单页前端（index.html + js/ 14 个 ESM 模块）
tests/     # 22 个离线测试文件（不触网、不需真实 Key）
docs/      # 全部文档（入口 docs/README.md）
data/      # 运行时数据（SQLite 库、头像、笔记图片，已 gitignore）
player.js  # 前后端播放器桥接文件
.env.example  # 配置模板（复制为 .env 使用）
```
