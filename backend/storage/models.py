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
    JSON, Boolean, Date, DateTime, Float, Index, Integer, String, Text, UniqueConstraint, false, true,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.storage.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Transcript(Base):
    """转写结果缓存（全局共享，按规范化 URL 的哈希为主键）。"""

    __tablename__ = "transcripts"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    url: Mapped[str] = mapped_column(Text, default="")
    normalized_url: Mapped[str] = mapped_column(Text, default="", index=True)
    title: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(16), default="")  # manual / auto / asr
    language: Mapped[str] = mapped_column(String(32), default="")
    language_name: Mapped[str] = mapped_column(String(64), default="")
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    segments: Mapped[list[Any]] = mapped_column(JSON, default=list)
    text: Mapped[str] = mapped_column(Text, default="")
    asr_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    webpage_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Summary(Base):
    """AI 总结缓存（按 文本哈希 + 模型 + 提示词版本 为主键，跨 URL 复用）。"""

    __tablename__ = "summaries"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(128), default="")
    prompt_version: Mapped[str] = mapped_column(String(16), default="")
    title: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Mindmap(Base):
    """AI 思维导图缓存（按 文本哈希 + 模型 + 提示词版本 为主键，跨 URL 复用）。

    与 Summary 同构：不设 TTL，键含模型与提示词版本，任一变更即自然失效。
    """

    __tablename__ = "mindmaps"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(128), default="")
    prompt_version: Mapped[str] = mapped_column(String(16), default="")
    title: Mapped[str] = mapped_column(Text, default="")
    mindmap: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

class Comment(Base):
    """高赞评论缓存（按规范化 URL 的哈希为主键）

    支持列注释的数据库（Postgres/MySQL，阶段1 目标）会在 DDL 中生成列注释；
    SQLite 无列注释语法，备注存于元数据，迁移后自动生效。
    """

    __tablename__ = "comments"
    __table_args__ = {"comment": "高赞评论缓存表（TTL=COMMENTS_CACHE_HOURS）"}

    # 缓存主键：comments_key(url) 的 sha256（域前缀 + 规范化 URL），同一视频唯一
    key: Mapped[str] = mapped_column(
        String(64), primary_key=True,
        comment="缓存主键：comments_key(url) 的 sha256（域前缀+规范化URL）",
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
        JSON, default=list,
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
    """

    __tablename__ = "video_infos"
    __table_args__ = {"comment": "视频信息缓存表（TTL=INFO_CACHE_HOURS）"}

    key: Mapped[str] = mapped_column(
        String(64), primary_key=True, comment="缓存主键：transcript_key(url)",
    )
    url: Mapped[str] = mapped_column(Text, default="", comment="原始视频链接（未规范化）")
    normalized_url: Mapped[str] = mapped_column(
        Text, default="", index=True, comment="规范化视频链接（跨链接形式命中同一缓存）",
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, comment="解析结果快照：标题/封面/时长/清晰度列表等",
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
        JSON, default=list, comment="已生成内容：transcribe/summary/mindmap/comments/qa 子集",
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