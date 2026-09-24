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

from sqlalchemy import JSON, Boolean, Date, DateTime, Integer, String, Text, false
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