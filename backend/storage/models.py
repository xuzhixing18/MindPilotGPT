"""ORM 模型：内容缓存（转写/总结/导图/评论）与多租户认证（用户/会话）。

用 SQLAlchemy 通用 ``JSON`` 类型（SQLite/Postgres 均可用；阶段1 迁 Postgres 时
可平滑换成 JSONB 以获得索引与更强的查询能力）。表结构对上层透明，业务层只经
``repo`` / ``auth.store`` 读写，不直接接触 ORM 对象。

表结构变更由 ``storage.migrations`` 在启动时对齐（加列 / SQLite 约束重建），
因此修改本文件后无需手工改库。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON, Boolean, Date, DateTime, Float, Index, Integer, String, Text,
    UniqueConstraint, false, true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.storage.db import Base

# JSON 列：Postgres 用 JSONB（可建 GIN 索引、支持 ->/->> 路径查询），SQLite 等方言回退通用 JSON
JSONCol = JSON().with_variant(JSONB, "postgresql")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Transcript(Base):
    """转写结果缓存（全局共享，按规范化 URL 的哈希去重，跨链接形式命中同一缓存）。

    ``id`` 为自增代理主键；业务键 ``key`` 保留为唯一约束——去重/命中语义仍由它承载，
    id 不参与任何业务寻址（本表无任何外键引用，纯点查缓存）。
    """

    __tablename__ = "transcripts"
    __table_args__ = (
        UniqueConstraint("key", name="uq_transcripts_key"),
        {"comment": "转写结果缓存表（TTL=TRANSCRIPT_CACHE_DAYS）"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="代理主键（自增）")
    key: Mapped[str] = mapped_column(
        String(64),
        comment="缓存业务键：transcript_key(url) 的 sha256（跨链接形式归一，唯一约束承载去重）",
    )
    url: Mapped[str] = mapped_column(Text, default="")
    normalized_url: Mapped[str] = mapped_column(Text, default="", index=True)
    title: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(16), default="")  # manual / auto / asr
    language: Mapped[str] = mapped_column(String(32), default="")
    language_name: Mapped[str] = mapped_column(String(64), default="")
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    segments: Mapped[list[Any]] = mapped_column(JSONCol, default=list)
    text: Mapped[str] = mapped_column(Text, default="")
    asr_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    webpage_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AiArtifact(Base):
    """AI 生成内容缓存（总结 / 思维导图）：按 (kind, 内容哈希) 区分内容身份，跨 URL 复用。

    合并产物：原 ``summaries`` / ``mindmaps`` 两表列结构完全一致，合并为一张表
    以 ``kind`` 区分（``summary`` / ``mindmap``），payload 统一承载生成内容本体。
    与转写/评论缓存不同族：键锚定「文本×模型×prompt 版本」，不设 TTL，任一维度
    变更即自然失效（旧版本行由运营清扫）。

    ``id`` 为自增代理主键；业务键 ``(kind, key)`` 保留为唯一约束——跨类隔离与去重仍由它承载。

    旧两表数据由 ``storage.migrations`` 在启动时一次性并入本表后删旧表（幂等）。
    """

    __tablename__ = "ai_artifacts"
    __table_args__ = (
        UniqueConstraint("kind", "key", name="uq_ai_artifacts_kind_key"),
        {"comment": "AI 生成内容缓存（kind=summary/mindmap，键含模型与提示词版本）"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="代理主键（自增）")
    kind: Mapped[str] = mapped_column(
        String(16), comment="内容种类：summary / mindmap",
    )
    key: Mapped[str] = mapped_column(
        String(64), comment="内容身份键：sha256(文本+模型+提示词版本)",
    )
    model: Mapped[str] = mapped_column(String(128), default="")
    prompt_version: Mapped[str] = mapped_column(String(16), default="")
    title: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONCol, default=dict, comment="生成内容本体（总结结构化字段或导图树）",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

class Comment(Base):
    """高赞评论缓存（按规范化 URL 的哈希去重）

    ``id`` 为自增代理主键；业务键 ``key`` 保留为唯一约束——去重/命中语义仍由它承载，
    id 不参与任何业务寻址（本表无任何外键引用，纯点查缓存）。
    支持列注释的数据库（Postgres/MySQL，阶段1 目标）会在 DDL 中生成列注释；
    SQLite 无列注释语法，备注存于元数据，迁移后自动生效。
    """

    __tablename__ = "comments"
    __table_args__ = (
        UniqueConstraint("key", name="uq_comments_key"),
        {"comment": "高赞评论缓存表（TTL=COMMENTS_CACHE_HOURS）"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="代理主键（自增）")
    # 缓存业务键：comments_key(url) 的 sha256（域前缀 + 规范化 URL），唯一约束承载去重
    key: Mapped[str] = mapped_column(
        String(64),
        comment="缓存业务键：comments_key(url) 的 sha256（域前缀+规范化URL）",
    )
    # 原始视频链接（用户输入/抓取时的 URL，未规范化）
    url: Mapped[str] = mapped_column(Text, default="", comment="原始视频链接（未规范化）")
    # 规范化视频链接（去跟踪参/小写 host/排序 query），跨链接形式命中同一缓存
    normalized_url: Mapped[str] = mapped_column(
        Text, default="", index=True,
        comment="规范化视频链接（去跟踪参数，用于跨链接形式缓存命中）",
    )
    # 视频标题（展示与排查用，可为空）
    title: Mapped[str] = mapped_column(Text, default="", comment="视频标题")
    source: Mapped[str] = mapped_column(
        String(32), default="",
        comment="来源平台标识：bilibili / douyin / generic",
    )
    total: Mapped[int] = mapped_column(Integer, default=0, comment="高赞评论条数（TopN 截断后）")
    comments: Mapped[list[Any]] = mapped_column(
        JSONCol, default=list,
        comment="高赞评论 JSON 列表：[{author,text,likes,time}]，按点赞降序",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow,
        comment="写入/刷新时间（UTC），用于 TTL 过期判断",
    )

class VideoInfo(Base):
    """视频信息缓存（全局共享）：/api/info 解析结果快照，避免重复 yt-dlp 提取。

    键与转写一致（``transcript_key(url)``），使历史/结果页二次打开秒回；
    TTL 由 ``INFO_CACHE_HOURS`` 控制（默认 24h）。下载仍走实时解析，保证直链新鲜。

    ``id`` 为自增代理主键；业务键 ``key`` 保留为唯一约束（与其余缓存表形状一致），
    id 不参与任何业务寻址（本表无任何外键引用，纯点查缓存）。
    """

    __tablename__ = "video_infos"
    __table_args__ = (
        UniqueConstraint("key", name="uq_video_infos_key"),
        {"comment": "视频信息缓存表（TTL=INFO_CACHE_HOURS）"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="代理主键（自增）")
    key: Mapped[str] = mapped_column(
        String(64), comment="缓存业务键：transcript_key(url)（唯一约束承载去重）",
    )
    url: Mapped[str] = mapped_column(Text, default="", comment="原始视频链接（未规范化）")
    normalized_url: Mapped[str] = mapped_column(
        Text, default="", index=True, comment="规范化视频链接（跨链接形式命中同一缓存）",
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONCol, default=dict, comment="解析结果快照：标题/封面/时长/清晰度列表等",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, comment="写入/刷新时间（UTC），用于 TTL 过期判断",
    )


class AIModel(Base):
    """AI 模型目录（阶段3 DB 化）：「选择模型」货架的持久化数据源。

    目录不再硬编码于 ai.config.MODEL_CATALOG——该常量降级为「出厂种子」，首次
    访问时导入本表（幂等，只补缺不覆盖管理员改动）。运营可经管理端点在线
    上下架 / 改展示名 / 调计费倍率，无需发版；用户侧校验与弹窗渲染均读本表
    （进程内缓存见 ai.catalog）。

    ``price_multiplier`` 为套餐/用量计费联动的预留位：阶段1 仅展示 tier 档位，
    接入 usage_counters 后按 (token 用量 × 倍率) 计量。
    """

    __tablename__ = "ai_models"
    __table_args__ = (
        UniqueConstraint("provider", "model", name="uq_ai_models_provider_model"),
        Index("ix_ai_models_provider_sort", "provider", "sort_order"),
        {"comment": "AI 模型目录（管理端可增删改，出厂种子见 ai.config.MODEL_CATALOG）"},
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, comment="uuid4 hex")
    provider: Mapped[str] = mapped_column(String(32), comment="服务商键（config.PROVIDERS 预设键）")
    model: Mapped[str] = mapped_column(String(128), comment="API 模型标识（LLM 请求体的 model 字段）")
    label: Mapped[str] = mapped_column(String(64), default="", server_default="", comment="弹窗展示名")
    tier: Mapped[str] = mapped_column(
        String(8), default="$", server_default="$", comment="价格档位徽标：$ / $$",
    )
    price_multiplier: Mapped[float] = mapped_column(
        Float, default=1.0, server_default="1.0", comment="计费倍率（套餐/用量计费联动预留）",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), comment="是否上架可选（false=隐藏但保留历史引用）",
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", comment="同服务商内展示排序（小在前）",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )


class User(Base):
    """用户（多租户认证主体 + 个人资料）。

    登录标识符支持**邮箱或手机号**（二者至少其一，由 service 层校验）；两列均为
    ``nullable + unique``——SQL 标准下 NULL 不参与唯一性比较，故多个「只有手机号」
    的用户不会互相冲突（SQLite / Postgres / MySQL 行为一致）。

    ``org_id`` 与组织角色、``plan_id`` 对应的 plans 表与权益（限流/配额轮次）均预留，
    当前 ``plan_id`` 仅作字符串标记。``email_verified`` / ``phone_verified`` 为验证能力
    预留（邮件确认 / 短信验证码轮次接入），当前注册均为 False。
    """

    __tablename__ = "users"
    __table_args__ = {"comment": "用户表（多租户主体 + 个人资料）"}

    id: Mapped[str] = mapped_column(String(32), primary_key=True, comment="用户 ID（uuid4 hex）")

    # ---- 登录标识符：邮箱 / 手机号（至少其一）----
    email: Mapped[str | None] = mapped_column(
        String(255), unique=True, index=True, nullable=True, comment="登录邮箱（统一小写），可空",
    )
    phone: Mapped[str | None] = mapped_column(
        String(20), unique=True, index=True, nullable=True,
        comment="登录手机号（E.164，如 +8613800138000），可空",
    )
    password_hash: Mapped[str] = mapped_column(String(255), default="", comment="密码哈希（argon2id/PBKDF2）")

    # ---- 个人资料 ----
    nickname: Mapped[str] = mapped_column(Text, default="", comment="展示昵称")
    avatar_url: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="头像地址（本站相对路径或第三方 URL），空表示未设置",
    )
    bio: Mapped[str] = mapped_column(
        Text, default="", server_default="", comment="个性签名",
    )
    gender: Mapped[str] = mapped_column(
        String(16), default="unknown", server_default="unknown",
        comment="性别：unknown / male / female",
    )
    birthday: Mapped[date | None] = mapped_column(Date, nullable=True, comment="生日（仅到月时日补 1）")
    location: Mapped[str] = mapped_column(
        String(64), default="", server_default="", comment="所在地区",
    )
    website: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="个人网站（仅 http/https，防 javascript: 注入）",
    )

    # ---- 状态、验证与锁定 ----
    status: Mapped[str] = mapped_column(
        String(16), default="active", comment="账号状态：active / locked / disabled",
    )
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, comment="邮箱是否已验证")
    phone_verified: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), comment="手机号是否已验证",
    )
    plan_id: Mapped[str] = mapped_column(String(32), default="free", comment="当前套餐标识：free/pro/team")
    org_id: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="组织归属（阶段C）")

    ai_provider: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="用户选择的 AI 服务商（为NULL则跟随全局默认）",
    )
    ai_model: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="用户选择的模型标识（为NULL则选默认服务商）",
    )
    privacy_mode: Mapped[bool] = mapped_column(
        Boolean, default=False, comment="隐私模式：不写入/不读取全局共享内容缓存",
    )
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, comment="连续登录失败次数")
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="锁定到期时间（UTC），空表示未锁定",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )


class UserSession(Base):
    """服务端会话（可撤销）。主键为会话令牌 token 的 sha256 哈希，原始 token 只进 Cookie。

    阶段A 用不透明随机会话令牌 + DB 存储（天然可即时撤销）；阶段B 可平滑换 JWT + Redis。
    表名用 ``sessions``，ORM 类名用 ``UserSession`` 以避免与 SQLAlchemy ``Session`` 混淆。
    """

    __tablename__ = "sessions"
    __table_args__ = {"comment": "用户会话表（服务端可撤销）"}

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, comment="会话令牌 token 的 sha256 hex（不存明文）",
    )
    user_id: Mapped[str] = mapped_column(String(32), index=True, default="", comment="归属用户")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, comment="过期时间（UTC）")
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="撤销时间（登出/踢下线），空表示有效",
    )
    user_agent: Mapped[str] = mapped_column(Text, default="", comment="创建时的 UA（设备列表展示）")
    ip: Mapped[str] = mapped_column(String(64), default="", comment="创建时的客户端 IP")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class UserHistory(Base):
    """处理历史（私有）：每 (用户, 视频) 一行，``kinds`` 聚合已生成的内容。

    ``content_key`` 取 ``transcript_key(url)`` 作为视频身份键，**只记归属与时间线、
    不复制内容**；title/url 为快照，全局缓存过期后历史列表仍可读。
    """

    __tablename__ = "user_history"
    __table_args__ = (
        UniqueConstraint("user_id", "content_key", name="uq_user_history_user_content"),
        Index("ix_user_history_user_updated", "user_id", "updated_at"),
        {"comment": "用户处理历史（私有，按 user_id 列级隔离）"},
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, comment="uuid4 hex")
    user_id: Mapped[str] = mapped_column(String(32), comment="归属用户")
    content_key: Mapped[str] = mapped_column(String(64), default="", comment="视频身份键 = transcript_key(url)")
    url: Mapped[str] = mapped_column(Text, default="", comment="原始链接快照（结果页回用）")
    title: Mapped[str] = mapped_column(Text, default="", comment="标题快照（缓存过期仍可读）")
    kinds: Mapped[list[Any]] = mapped_column(
        JSONCol, default=list, comment="已生成内容：transcribe/summary/mindmap/comments/qa 子集",
    )
    source: Mapped[str] = mapped_column(String(32), default="", comment="来源平台：bilibili / douyin / generic")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, comment="最近活跃时间（列表排序键）",
    )


class QaSession(Base):
    """问答会话（私有）：同一视频可多会话；消息在 qa_messages 表 append-only 持久化。"""

    __tablename__ = "qa_sessions"
    __table_args__ = (
        Index("ix_qa_sessions_user_content", "user_id", "content_key"),
        Index("ix_qa_sessions_user_updated", "user_id", "updated_at"),
        {"comment": "用户问答会话（私有，跨设备续聊）"},
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, comment="uuid4 hex")
    user_id: Mapped[str] = mapped_column(String(32), comment="归属用户")
    content_key: Mapped[str] = mapped_column(String(64), default="", comment="视频身份键")
    title: Mapped[str] = mapped_column(Text, default="", comment="会话名（默认视频标题，可改名）")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, comment="最近一轮问答时间",
    )


class QaMessage(Base):
    """问答消息（append-only）：单条内容服务端截断 ≤2000 字。"""

    __tablename__ = "qa_messages"
    __table_args__ = (
        Index("ix_qa_messages_session_created", "session_id", "created_at"),
        {"comment": "问答消息（append-only，随会话级联删除）"},
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, comment="uuid4 hex")
    session_id: Mapped[str] = mapped_column(String(32), comment="归属会话")
    role: Mapped[str] = mapped_column(String(16), default="user", comment="user / assistant")
    content: Mapped[str] = mapped_column(Text, default="", comment="消息内容")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Collection(Base):
    """合集（私有）：用户对视频及其解析内容的主动组织。"""

    __tablename__ = "collections"
    __table_args__ = {"comment": "用户合集（私有）"}

    id: Mapped[str] = mapped_column(String(32), primary_key=True, comment="uuid4 hex")
    user_id: Mapped[str] = mapped_column(String(32), index=True, comment="归属用户")
    name: Mapped[str] = mapped_column(String(40), default="", comment="合集名（≤40 字）")
    description: Mapped[str] = mapped_column(Text, default="", comment="可选描述")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )


class CollectionItem(Base):
    """合集条目：快照 title/url，与历史行解耦（删历史不影响合集展示）。"""

    __tablename__ = "collection_items"
    __table_args__ = (
        UniqueConstraint("collection_id", "content_key", name="uq_collection_item"),
        {"comment": "合集条目（同一合集内视频唯一）"},
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, comment="uuid4 hex")
    collection_id: Mapped[str] = mapped_column(String(32), index=True, comment="归属合集")
    content_key: Mapped[str] = mapped_column(String(64), default="", comment="视频身份键")
    url: Mapped[str] = mapped_column(Text, default="", comment="原始链接快照")
    title: Mapped[str] = mapped_column(Text, default="", comment="标题快照")
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class UserNote(Base):
    """随手笔记（私有）：同一视频允许多篇，``body`` 为 Markdown 源文唯一事实源。

    与 ``UserHistory`` 只记归属不同，笔记是**内容本体**：正文、图片计数、时间戳
    索引都存在本表。``marks`` 由服务端从 ``body`` 解析生成（不信任客户端上报），
    供时间线聚合与导出免全文正则。``updated_at`` 兼作自动保存的乐观并发基线。
    """

    __tablename__ = "user_notes"
    __table_args__ = (
        Index("ix_user_notes_user_updated", "user_id", "updated_at"),
        Index("ix_user_notes_user_content", "user_id", "content_key"),
        {"comment": "用户随手笔记（私有，一视频多篇，按 user_id 列级隔离）"},
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, comment="uuid4 hex")
    user_id: Mapped[str] = mapped_column(String(32), comment="归属用户")
    content_key: Mapped[str] = mapped_column(String(64), default="", comment="视频身份键 = transcript_key(url)")
    url: Mapped[str] = mapped_column(Text, default="", comment="视频链接快照（缓存过期后笔记仍可读）")
    title: Mapped[str] = mapped_column(Text, default="", comment="视频标题快照")
    body: Mapped[str] = mapped_column(Text, default="", comment="Markdown 源文（唯一事实源，前端渲染）")
    marks: Mapped[list[Any]] = mapped_column(
        JSONCol, default=list,
        comment="时间戳索引 [{t,label}]，写库时由服务端从 body 解析",
    )
    tag: Mapped[str] = mapped_column(
        String(16), default="", server_default="",
        comment="语义标签：空 / key重点 / question疑问 / idea灵感（P1 过滤用，P0 预留）",
    )
    starred: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), comment="收藏（P1 预留）")
    source_type: Mapped[str] = mapped_column(
        String(16), default="manual", server_default="manual",
        comment="来源：manual / subtitle / chapter / qa / ai_draft",
    )
    source_ref: Mapped[str] = mapped_column(String(64), default="", server_default="", comment="引用来源锚点（如 qa_messages.id），仅溯源展示")
    image_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", comment="图片数冗余计数（列表卡片免 join）")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        comment="最近保存时间（列表排序键 + 自动保存冲突基线）",
    )


class NoteImage(Base):
    """笔记图片（私有）：独立落盘而非 base64 塞正文，支持配额与孤儿清扫。

    ``id`` 即文件名（uuid4 hex，防路径穿越）；``note_id`` 可空——「新建未保存时
    贴图」的图片先落地，由 service 在笔记首次保存时认领。``t`` 为截图对应的
    视频时间点（用户贴图为 NULL）。"""

    __tablename__ = "note_images"
    __table_args__ = (
        Index("ix_note_images_user_created", "user_id", "created_at"),
        Index("ix_note_images_note", "note_id"),
        {"comment": "笔记图片（私有，文件存 data/note_images，读取需鉴权）"},
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, comment="uuid4 hex，同时是文件名")
    user_id: Mapped[str] = mapped_column(String(32), comment="归属用户")
    note_id: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="归属笔记；空=尚未被认领（孤儿候选）")
    content_key: Mapped[str] = mapped_column(String(64), default="", comment="视频身份键（认领与清扫依据）")
    t: Mapped[float | None] = mapped_column(Float, nullable=True, comment="截图时间点（秒）；非截图为 NULL")
    mime: Mapped[str] = mapped_column(String(32), default="", comment="由魔数判定的真实 MIME")
    bytes: Mapped[int] = mapped_column(Integer, default=0, comment="体积（配额与清扫依据）")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, comment="孤儿判定依据")