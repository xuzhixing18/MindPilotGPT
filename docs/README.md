# MindPilot 项目文档导航与快速上手

> 本文是 MindPilot 项目的**文档总入口**：3 分钟了解项目是什么 → 10 分钟读懂架构与代码地图 → 按需跳转到各专题文档。
> 当前代码基线：2026-10-18（含分享 / 随手笔记 / SEO / AI 模型目录等全部已落地能力）。

---

## 一、项目是什么（一句话 + 一张图）

**MindPilot 是一个「万能视频解析下载 + AI 分析」的 Web 工具**：粘贴链接即可解析视频，
支持下载、字幕转写、AI 总结 / 思维导图 / 多轮问答、高赞评论抓取；登录用户还能沉淀处理历史、
合集、QA 会话与随手笔记，并把分析结果以短链分享出去。

```
粘贴链接 → 解析信息(秒回缓存) → [下载] [在线播放] [字幕] [AI总结] [思维导图] [高赞评论] [AI问答] [随手笔记]
                │
                └─ 登录用户：自动沉淀处理历史 → 合集组织 → QA 会话跨设备续聊 → 笔记随视频分享
```

| 维度 | 现状 |
|------|------|
| 定位 | 工具层（解析/下载/转写）立身，智能层（总结/导图/问答）差异化，资产层（历史/合集/会话/笔记）留存，分享/SEO 获客 |
| 技术栈 | 后端 Python 3.11 + FastAPI + SQLAlchemy + yt-dlp；前端原生 ESM 单页（零构建零依赖）+ Tailwind CDN |
| 存储 | 默认 SQLite（WAL）单文件；改 `DATABASE_URL` 一行即迁 Postgres（ORM 已抽象） |
| 架构纪律 | 门面 + 可插拔注册表、缓存优先 + single-flight、密钥只来自环境、异常语义化、离线可测 |
| 工程规模 | 后端 10 个能力包；前端 14 个 JS 模块；15 张 ORM 表；22 个离线测试文件 |

---

## 二、按阅读目的选择文档

### 第一次接触（推荐顺序）

| 顺序 | 文档 | 看什么 |
|------|------|--------|
| 1 | 本文（README） | 项目全景、代码地图、启动方式 |
| 2 | [项目实现总结.md](./项目实现总结.md) | **当前版本的完整实现**：10 个能力包逐一拆解、全部 HTTP API、15 张表、测试清单、配置项 |
| 3 | [项目全景分析与技术评审报告.md](./项目全景分析与技术评审报告.md) | 架构剖析、设计亮点、**问题清单**（P0 安全 / P1 扩展 / P2 AI / P3 工程化）与演进路线图 |
| 4 | [项目需求说明.md](./项目需求说明.md) | 需求清单与实现落点对照、平台支持矩阵、架构原则（约束后续迭代） |

### 按角色 / 场景

| 角色 / 场景 | 文档 |
|-------------|------|
| 接手后端开发 | 实现总结（模块拆解）→ 全景分析报告（架构原则 + 问题清单）→ 对应能力包的设计方案 |
| 接手前端开发 | 实现总结 §前端架构；`frontend/js/` 各模块顶部注释即文档 |
| 做产品 / 功能规划 | 竞品分析与功能规划 → 需求说明 → 全景分析报告（SaaS 产品化维度） |
| 查某功能的实现细节 | 下表的「设计方案 / 实现记录」文档 |
| 排查线上问题 | [bug排查记录/](./bug排查记录/) 两个既有案例（B 站评论风控、LLM JSON 解析）|
| 做 SEO / 增长 | [SEO优化工作流.md](./SEO优化工作流.md)（规范）→ [GEO优化入门指南.md](./GEO优化入门指南.md)（AI 引擎优化）→ 实现总结 §seo 包 |

### 功能 ↔ 文档对照表

| 功能 | 后端包 | 前端 | 设计方案 / 实现记录 |
|------|--------|------|--------------------|
| 视频解析 / 下载 / 在线播放 | `downloader/` + `main.py` `/api/stream` | views/parse.js、player.js、views/result.js | 实现总结 §二、[历史记录与合集管理实现方案 §1.7](./历史记录与合集管理实现方案.md)（播放器） |
| 字幕转写（字幕 → ASR 链） | `transcribe/` | views/result.js | 实现总结 §三 |
| AI 总结 / 导图 / 问答 | `ai/` | views/result.js | 实现总结 §四、[AI模型选择与默认模型设置实现方案](./AI模型选择与默认模型设置实现方案.md) |
| 高赞评论 | `comments/` | views/result.js | 实现总结 §五、[B站评论抓取问题记录](./bug排查记录/B站评论抓取问题记录.md) |
| 认证（注册/登录/资料/头像） | `auth/` | auth-ui.js | 实现总结 §六、[多租户与限流技术方案](./多租户与限流技术方案.md)（阶段A 已落地） |
| 历史 / 合集 / QA 会话 | `library/` | sidebar.js、views/history.js、views/collections.js | [历史记录与合集管理实现方案](./历史记录与合集管理实现方案.md) |
| 随手笔记（含截图/抽帧） | `notes/` | views/notes.js | [随手笔记功能设计方案](./随手笔记功能设计方案.md)（含实现记录） |
| 分享（短链/海报/落地页） | `share/` | share.js | [分享功能设计方案](./分享功能设计方案.md)（v1.1~v1.3 实现记录） |
| SEO / GEO 基础设施 | `seo/` | index.html（TDK） | [SEO优化工作流](./SEO优化工作流.md)、[GEO优化入门指南](./GEO优化入门指南.md) |
| AI 模型目录（选择模型） | `ai/catalog.py` + `ai_models` 表 + 管理端点 | model-picker.js | [AI模型选择与默认模型设置实现方案](./AI模型选择与默认模型设置实现方案.md)（注：目录已 DB 化） |
| 存储 / 缓存 / 迁移 | `storage/` | — | 实现总结 §九 |

---

## 三、代码地图（backend/ 10 包 + frontend/ 14 模块）

```
backend/
├── main.py          # 应用入口：业务端点 + 三态门禁 _AUTH_GATE + /api/stream + 管理端点 + 静态托管
├── storage/         # 底座：db(engine/WAL) + models(15表) + keys(缓存键) + repo(TTL) + singleflight + migrations(声明式)
├── downloader/      # 解析下载：registry(wechat_channels拦截/douyin/kuaishou) → generic(yt-dlp) 兜底
├── transcribe/      # 转写两级：subtitles(字幕优先) → audio+asr(Fun-ASR→硅基→Qwen3-ASR 链) 兜底
├── ai/              # LLM 层：config(4 服务商) + llm(OpenAI兼容+降级重试) + jsonx(JSON修复) + catalog(模型目录DB)
│                    #        + summary / mindmap(含xmind导出+章节时间线) / qa
├── comments/        # 高赞评论：bilibili / douyin / kuaishou / ximalaya → generic（无 LLM，TTL 缓存）
├── auth/            # 认证：邮箱/手机号双通道 + 会话 + 防枚举/锁定/限流 + 资料/头像/ai-settings
├── library/         # 私有资源：历史/合集/QA 会话（/api/me/*，user_id 列级隔离）
├── notes/           # 随手笔记：正文/图片/抽帧 + 自动保存冲突(409) + SSRF 防护
├── share/           # 分享：短链 + SSR 落地页 + 封面代理 + QR + 限流 + view 去重
└── seo/             # SEO：robots.txt / llms.txt / sitemap.xml + SPA 壳动态渲染 + 品牌化 404

frontend/js/
├── main.js          # bootstrap（唯一同时 import router 与视图的地方，规避循环依赖）
├── core.js          # 工具/请求/缓存 Map/bus 事件/401 处理
├── router.js        # hash 路由注册表（#/ #/v #/history #/search #/collections #/c/:id）
├── sidebar.js       # 侧边栏：搜索联想/合集/历史/登录引导卡
├── auth-ui.js       # 登录框 + 个人中心 + 默认模型入口
├── model-picker.js  # 选择模型弹窗
├── coll-picker.js   # 加入合集 popover
├── share.js         # 分享弹窗 + 海报 canvas + 多渠道
├── player.js        # 三形态播放器（原生流播 / B站 iframe / YouTube iframe）
└── views/           # parse(解析首页) result(结果页六Tab,1449行) history collections notes
```

**贯穿全项目的五条架构原则**（详见[项目需求说明.md §五](./项目需求说明.md)）：
1. **门面 + 可插拔**：新增平台/服务商只改注册表一行，不改门面与前端；
2. **LLM 边界纪律**：确定性 IO（抓取/排序/解析）绝不调 LLM；
3. **缓存优先 + 并发去重**：所有昂贵结果落库缓存 + single-flight，二次访问秒开；
4. **密钥只来自环境**：`.env` 已被 gitignore，`.env.example` 只留占位符；
5. **前端零构建零依赖**：原生 ES Modules + Canvas/SVG 自绘。

---

## 四、快速启动与验证

```powershell
# 1. 依赖（Python 3.11+）
pip install -r requirements.txt

# 2. 配置：复制 .env.example 为 .env 按需填写（不配任何 Key 也能跑下载/字幕提取）
copy .env.example .env

# 3. 启动（默认 0.0.0.0:8010，前端由 FastAPI StaticFiles 托管在根路径）
python -m backend.main
# 或：uvicorn backend.main:app --reload --host 0.0.0.0 --port 8010

# 4. 健康检查
#    GET http://127.0.0.1:8010/api/health
#    → {status, ffmpeg, ai, ai_provider, asr, asr_provider, db, auth, auth_required}

# 5. 跑测试（全部离线，不触网、不需真实 Key；逐文件独立进程跑，避免共库污染）
.venv\Scripts\python.exe tests\test_smoke.py
.venv\Scripts\python.exe tests\test_library.py
# 其余同理，见 实现总结 §测试
```

**最小可用配置**（全部可选）：`DEEPSEEK_API_KEY`（AI 总结/导图/问答）、`SILICONFLOW_API_KEY`（免费 ASR 兜底）、
`DASHSCOPE_API_KEY`（Fun-ASR 带时间戳转写）、`COOKIES_FROM_BROWSER=chrome`（B 站语音字幕）、
`PUBLIC_BASE_URL`（分享短链域名）、`ADMIN_API_TOKEN`（AI 模型目录管理端点）。
完整清单见 `.env.example`（含注释）与 [项目实现总结.md §配置](./项目实现总结.md)。

---

## 五、当前已知问题与下一步（要点摘录）

> 完整清单与证据见[全景分析报告 §五](./项目全景分析与技术评审报告.md)，此处只列结论。

| 优先级 | 问题 | 状态（2026-10-18 复核） |
|--------|------|------------------------|
| P0-1 | SSRF：`/api/info`、`/api/transcribe` 等接受任意 URL 服务端直连 | **仍开放**（notes/frames 与 share/thumb 已做防护，主链路未收口） |
| P0-2 | CORS 全开 `allow_origins=["*"]` | **仍开放** |
| P0-3 | 异常文本直接回传前端（`detail=f"下载出错：{exc}"`） | **仍开放** |
| P1-1 | 同步阻塞长任务（转写/ASR/LLM）占满线程池 | 仍开放（阶段1 任务队列） |
| P1-2 | 业务端点零限流零配额；plan_id 死字段；无用量计量 | 仍开放（分享/登录端点已有局部限流） |
| P2-1 | 长文本暴力截断（30000 字符取前半） | 仍开放（分块/RAG 规划中） |
| P2-2 | LLM JSON 解析偶发失败 | **已修复**（`ai/jsonx.py` 三级修复：多候选提取→本地修复→模型修复重试） |
| P2-3 | 导图不可编辑 | 部分缓解（新增 XMind 导出，可导入外部工具编辑） |
| P3-1 | `result.js` 1449 行，超零构建前端可维护拐点 | **恶化中**（评审时为 670 行），建议按 Tab 拆子模块 |

---

## 六、文档索引（全部）

| 文档 | 类型 | 更新状态 |
|------|------|----------|
| [README.md](./README.md)（本文） | 导航 | ✅ 2026-10-18 |
| [项目实现总结.md](./项目实现总结.md) | 实现总结 | ✅ 2026-10-18 重写（覆盖全部 10 包） |
| [项目需求说明.md](./项目需求说明.md) | 需求/路线图 | ✅ 2026-10-18 更新 |
| [项目全景分析与技术评审报告.md](./项目全景分析与技术评审报告.md) | 评审 | ✅ 2026-10-18 增补复核附录 |
| [历史记录与合集管理实现方案.md](./历史记录与合集管理实现方案.md) | 方案+实现 | ✅ 已落地 |
| [分享功能设计方案.md](./分享功能设计方案.md) | 方案+实现记录 | ✅ P0 已落地（v1.3） |
| [随手笔记功能设计方案.md](./随手笔记功能设计方案.md) | 方案+实现记录 | ✅ P0 已落地（P1 部分提前） |
| [AI模型选择与默认模型设置实现方案.md](./AI模型选择与默认模型设置实现方案.md) | 方案+实现 | ✅ 已落地；目录已进一步 DB 化 |
| [SEO优化工作流.md](./SEO优化工作流.md) | 规范参考 | 通用指南（seo 包据此实施） |
| [GEO优化入门指南.md](./GEO优化入门指南.md) | 规范参考 | 通用指南（llms.txt 等据此实施） |
| [bug排查记录/B站评论抓取问题记录.md](./bug排查记录/B站评论抓取问题记录.md) | 复盘 | 2026-09-28 |
| [bug排查记录/大模型调用问题记录.md](./bug排查记录/大模型调用问题记录.md) | 复盘 | 2026-09-26 |

**文档维护约定**（建议遵守）：
1. 每次功能迭代后，同步更新《项目实现总结.md》对应能力包小节 + 本文档「功能 ↔ 文档对照表」；
2. 每个新能力包落一份「设计方案」文档（含实现记录/偏差章节，参照分享、笔记方案）；
3. 线上 Bug 按 [bug排查记录](./bug排查记录/) 模板（现象→根因→排查→方案→验证→收获）追加；
4. 全景分析报告的「问题清单」每轮评审复核一次，已修复项标注状态，避免文档与现实漂移。
