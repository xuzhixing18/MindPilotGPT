"""轻量声明式迁移（storage.migrations）的离线测试。

为什么单独一个文件：迁移必须在**旧结构的库**上验证，而其余测试都跑在新建库上
（结构天然对齐，走不到迁移分支）。本用例先用裸 SQL 造出改造前的 users 表
（13 列、email NOT NULL、仅 ix_users_email），再让 ``init_db()`` 把它对齐到当前 ORM。

覆盖：
- 缺列补齐（8 个新列）与约束放宽（email NOT NULL → NULL，SQLite 走重建表）
- 唯一索引同步（ix_users_email / ix_users_phone 均 unique）——这是手机号唯一性的唯一承载
- 数据零丢失 + 一次性数据修正（email='' → NULL，否则多个纯手机号账号会撞唯一索引）
- 幂等（二次执行 APPLIED 为空）与重建前自动备份 *.pre-migration.bak
- 方案B 表级合并：旧 summaries/mindmaps 数据并入 ai_artifacts 后删旧表，
  按 (kind, key) 主键防重，payload 原样保留，二次执行无动作
- 缓存表加自增代理主键：transcripts/video_infos 旧结构（业务键为主键）→ id 主键，
  业务键降级为唯一约束，存量数据与 payload 零丢失、repo 读路径仍命中

运行：python tests/test_migrations.py
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

# 将项目根目录加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP_DIR = Path(tempfile.mkdtemp(prefix="mp_mig_test_"))
_DB_PATH = _TMP_DIR / "legacy.db"

# 关键：db.py 在「导入时」即按 DATABASE_URL 建 engine，必须先于任何 backend.* 导入设置
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH.as_posix()}"

# 改造前的 users 表（仅登录标识符为邮箱、无个人资料字段）
_LEGACY_DDL = """
CREATE TABLE users (
    id VARCHAR(32) NOT NULL,
    email VARCHAR(255) NOT NULL DEFAULT '',
    password_hash VARCHAR(255) NOT NULL,
    nickname TEXT NOT NULL,
    status VARCHAR(16) NOT NULL,
    email_verified BOOLEAN NOT NULL,
    plan_id VARCHAR(32) NOT NULL,
    org_id VARCHAR(32),
    privacy_mode BOOLEAN NOT NULL,
    failed_login_count INTEGER NOT NULL,
    locked_until DATETIME,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id)
);
CREATE UNIQUE INDEX ix_users_email ON users (email);
"""

_NEW_COLUMNS = {
    "phone", "avatar_url", "bio", "gender", "birthday", "location", "website", "phone_verified",
    "ai_provider", "ai_model",
}


def _make_legacy_db() -> None:
    """造一个改造前的库：两行数据，其中一行 email 为空串（旧版默认值）。"""
    conn = sqlite3.connect(_DB_PATH)
    try:
        conn.executescript(_LEGACY_DDL)
        now = "2026-01-01 00:00:00"
        conn.executemany(
            "INSERT INTO users (id, email, password_hash, nickname, status, email_verified,"
            " plan_id, org_id, privacy_mode, failed_login_count, locked_until, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("a" * 32, "legacy@example.com", "pbkdf2_sha256$x", "老用户", "active",
                 0, "free", None, 0, 0, None, now, now),
                # email='' 是旧版「无邮箱」的写法，迁移后必须归一为 NULL
                ("b" * 32, "", "pbkdf2_sha256$y", "空邮箱用户", "active",
                 0, "free", None, 0, 0, None, now, now),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _columns() -> dict[str, dict]:
    conn = sqlite3.connect(_DB_PATH)
    try:
        rows = conn.execute("PRAGMA table_info(users)").fetchall()
        return {r[1]: {"nullable": not r[3], "default": r[4]} for r in rows}
    finally:
        conn.close()


def _indexes() -> dict[str, bool]:
    """显式索引名 → 是否唯一（主键自带的 sqlite_autoindex_* 不算，SQLAlchemy 也不管它）。"""
    conn = sqlite3.connect(_DB_PATH)
    try:
        rows = conn.execute("PRAGMA index_list(users)").fetchall()
        return {r[1]: bool(r[2]) for r in rows if not r[1].startswith("sqlite_autoindex_")}
    finally:
        conn.close()


def test_legacy_shape_before_migration():
    """前置断言：确认造的确实是「旧结构」，否则后面的迁移断言没有意义。"""
    _make_legacy_db()
    cols = _columns()
    assert len(cols) == 13, cols
    assert cols["email"]["nullable"] is False          # 旧版 email 是 NOT NULL
    assert not (_NEW_COLUMNS & set(cols)), "旧库不应含新列"
    assert _indexes() == {"ix_users_email": True}
    print("[mig] legacy db shape ok（13 列 / email NOT NULL / 仅邮箱唯一索引）")


def test_ensure_schema_aligns_structure():
    from backend import storage
    from backend.storage import migrations

    assert storage.init_db() is True
    applied = migrations.APPLIED
    assert applied, f"首次迁移应有动作，实际为空：{applied}"
    assert any("users: rebuilt" in a for a in applied), applied   # email 放宽须走重建

    cols = _columns()
    missing = _NEW_COLUMNS - set(cols)
    assert not missing, f"新列未补齐：{missing}"
    assert cols["email"]["nullable"] is True, "email 应已放宽为可空"
    # NOT NULL 新列必须有默认值，否则 SQLite 的 ADD COLUMN 会直接失败
    assert cols["gender"]["default"] == "'unknown'", cols["gender"]
    assert cols["bio"]["default"] == "''", cols["bio"]

    ix = _indexes()
    assert ix.get("ix_users_email") is True, ix        # 唯一索引须在重建后恢复
    assert ix.get("ix_users_phone") is True, ix        # 手机号唯一性由它承载
    assert not any(name.endswith("__mig_old") for name in ix), ix
    print(f"[mig] ensure_schema ok（新增 {len(_NEW_COLUMNS)} 列 + 重建放宽 email + 补唯一索引）")


def test_data_preserved_and_fixup_applied():
    conn = sqlite3.connect(_DB_PATH)
    try:
        rows = dict(conn.execute("SELECT id, email FROM users").fetchall())
        orphan = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%__mig_old'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 2, rows                        # 数据零丢失
    assert rows["a" * 32] == "legacy@example.com"
    assert rows["b" * 32] is None, "空串 email 应归一为 NULL（否则多个纯手机号账号会互撞）"
    assert not orphan, f"临时表未清理：{orphan}"
    assert (_DB_PATH.with_name(_DB_PATH.name + ".pre-migration.bak")).is_file(), "重建前应备份"
    print("[mig] data preserved + email '' → NULL + 备份文件 ok")


def test_multiple_null_emails_coexist():
    """SQL 标准：NULL 不参与唯一性比较 → 多个「只有手机号」的账号可共存。"""
    from sqlalchemy.exc import IntegrityError

    from backend.storage.db import session
    from backend.storage.models import User

    def insert(uid: str, phone: str) -> None:
        # 其余列交给 ORM 默认值（created_at 等），只显式给标识符
        with session() as s:
            s.add(User(id=uid, email=None, phone=phone, password_hash="h", nickname="手机用户"))

    insert("c" * 32, "+8613800138000")
    try:
        insert("d" * 32, "+8613900139000")   # 同样 email=NULL
    except IntegrityError as exc:            # pragma: no cover - 失败路径
        raise AssertionError(f"两个 NULL 邮箱账号应能共存：{exc}") from exc

    # 但重复手机号必须被唯一索引拦住（唯一性静默失效是本迁移最危险的回归）
    try:
        insert("e" * 32, "+8613800138000")
    except IntegrityError:
        pass
    else:
        raise AssertionError("重复手机号应被唯一索引拒绝")
    print("[mig] NULL email 可共存 + 重复手机号被拒 ok")


# 方案B 合并前的旧缓存表（结构 = 当年 ORM 所建，payload 列分别为 summary / mindmap）
_LEGACY_ARTIFACT_DDL = """
CREATE TABLE summaries (
    key VARCHAR(64) NOT NULL,
    model VARCHAR(128) NOT NULL,
    prompt_version VARCHAR(16) NOT NULL,
    title TEXT NOT NULL,
    summary JSON NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (key)
);
CREATE TABLE mindmaps (
    key VARCHAR(64) NOT NULL,
    model VARCHAR(128) NOT NULL,
    prompt_version VARCHAR(16) NOT NULL,
    title TEXT NOT NULL,
    mindmap JSON NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (key)
);
"""


def _make_legacy_artifact_tables() -> None:
    """在已对齐的库上造两张旧缓存表，各塞一行数据。"""
    conn = sqlite3.connect(_DB_PATH)
    try:
        conn.executescript(_LEGACY_ARTIFACT_DDL)
        now = "2026-01-01 00:00:00"
        conn.execute(
            "INSERT INTO summaries (key, model, prompt_version, title, summary, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("s" * 64, "deepseek · deepseek-chat", "v1", "旧总结标题",
             '{"one_line": "旧结论", "keywords": ["旧关键词"]}', now),
        )
        conn.execute(
            "INSERT INTO mindmaps (key, model, prompt_version, title, mindmap, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("m" * 64, "deepseek · deepseek-chat", "v1", "旧导图标题",
             '{"title": "旧导图", "children": [{"title": "旧分支", "children": []}]}', now),
        )
        conn.commit()
    finally:
        conn.close()


def _legacy_tables_remaining() -> list[str]:
    conn = sqlite3.connect(_DB_PATH)
    try:
        return sorted(
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name IN ('summaries', 'mindmaps')"
            ).fetchall()
        )
    finally:
        conn.close()


def test_ai_artifact_merge_from_legacy_tables():
    """旧 summaries/mindmaps → ai_artifacts：并入 + 删旧表 + payload 原样 + 幂等。"""
    from backend import storage
    from backend.storage import migrations, models

    _make_legacy_artifact_tables()
    actions = migrations.ensure_schema()
    assert any("summaries -> ai_artifacts" in a for a in actions), actions
    assert any("mindmaps -> ai_artifacts" in a for a in actions), actions
    assert _legacy_tables_remaining() == [], "旧表应已删除"

    # 数据并入 ai_artifacts：payload 原样保留，kind 正确（业务寻址走 (kind,key) 唯一索引）
    from sqlalchemy import select as _select

    with storage.session() as s:
        row = s.scalar(_select(models.AiArtifact).where(
            models.AiArtifact.kind == "summary", models.AiArtifact.key == "s" * 64))
        assert row is not None, "旧 summaries 行未并入"
        assert row.title == "旧总结标题"
        assert row.payload["one_line"] == "旧结论"
        assert row.payload["keywords"] == ["旧关键词"]
        row = s.scalar(_select(models.AiArtifact).where(
            models.AiArtifact.kind == "mindmap", models.AiArtifact.key == "m" * 64))
        assert row is not None, "旧 mindmaps 行未并入"
        assert row.payload["title"] == "旧导图"
        assert row.payload["children"][0]["title"] == "旧分支"

    # 二次执行：旧表已不存在，无任何动作（幂等）
    assert migrations.ensure_schema() == []
    print("[mig] ai_artifacts 合并旧 summaries/mindmaps 并删旧表 ok")


# 加自增 id 之前的旧 transcripts 结构（业务键 key 为主键，无 id 列）
_LEGACY_TRANSCRIPTS_DDL = """
CREATE TABLE transcripts (
    key VARCHAR(64) NOT NULL,
    url TEXT NOT NULL,
    normalized_url TEXT NOT NULL,
    title TEXT NOT NULL,
    source VARCHAR(16) NOT NULL,
    language VARCHAR(32) NOT NULL,
    language_name VARCHAR(64) NOT NULL,
    char_count INTEGER NOT NULL,
    segments JSON NOT NULL,
    text TEXT NOT NULL,
    asr_provider VARCHAR(64),
    webpage_url TEXT,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (key)
);
"""


# 加自增 id 之前的旧 video_infos 结构（业务键 key 为主键，无 id 列）
_LEGACY_VIDEO_INFOS_DDL = """
CREATE TABLE video_infos (
    key VARCHAR(64) NOT NULL,
    url TEXT NOT NULL,
    normalized_url TEXT NOT NULL,
    payload JSON NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (key)
);
"""


def _make_legacy_transcripts() -> None:
    """把已对齐的 transcripts 表替换为旧结构（无 id），塞两行数据。"""
    from sqlalchemy import text

    from backend.storage import db as sdb

    with sdb.engine.begin() as conn:
        conn.execute(text("DROP TABLE transcripts"))
        conn.execute(text(_LEGACY_TRANSCRIPTS_DDL))
        now = "2026-01-01 00:00:00"
        conn.execute(
            text("INSERT INTO transcripts (key, url, normalized_url, title, source, language,"
                 " language_name, char_count, segments, text, created_at)"
                 " VALUES (:k, :u, :n, :t, 'auto', 'zh', '自动字幕', 4, '[]', :x, :c)"),
            [
                {"k": "t" * 64, "u": "https://a/1", "n": "https://a/1", "t": "旧视频一",
                 "x": "字幕一", "c": now},
                {"k": "u" * 64, "u": "https://a/2", "n": "https://a/2", "t": "旧视频二",
                 "x": "字幕二", "c": now},
            ],
        )


def _make_legacy_video_infos() -> None:
    """把已对齐的 video_infos 表替换为旧结构（无 id、key 为主键），塞一行未过期数据。"""
    from datetime import datetime, timezone

    from sqlalchemy import text

    from backend.storage import db as sdb

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
    with sdb.engine.begin() as conn:
        conn.execute(text("DROP TABLE video_infos"))
        conn.execute(text(_LEGACY_VIDEO_INFOS_DDL))
        conn.execute(
            text("INSERT INTO video_infos (key, url, normalized_url, payload, created_at)"
                 " VALUES (:k, :u, :n, :p, :c)"),
            {"k": "v" * 64, "u": "https://a/1", "n": "https://a/1",
             "p": '{"title": "旧信息", "formats": []}', "c": now},
        )


def test_surrogate_id_migration():
    """旧结构缓存表（无 id，业务键为主键）→ 自增代理主键迁移：数据保留、唯一性转约束。"""
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from backend import storage
    from backend.storage import migrations, models

    _make_legacy_transcripts()
    actions = migrations.ensure_schema()
    assert any("transcripts: rebuilt" in a for a in actions), actions

    # 数据零丢失，id 自动回填且为新的代理主键
    with storage.session() as s:
        rows = s.scalars(select(models.Transcript).order_by(models.Transcript.id)).all()
        assert [(r.id, r.title, r.text) for r in rows] == [
            (1, "旧视频一", "字幕一"), (2, "旧视频二", "字幕二"),
        ], [(r.id, r.title) for r in rows]

    # 旧主键（业务键）降级为唯一约束：重复 key 必须被拒（唯一性静默失效是最危险的回归）
    try:
        with storage.session() as s:
            s.add(models.Transcript(key="t" * 64, url="x", text="重复键"))
        assert False, "重复业务键应被唯一约束拒绝"
    except IntegrityError:
        pass

    # 主键结构：PK 应为 id，且存在业务键唯一索引
    insp = __import__("sqlalchemy").inspect(storage.migrations.engine)
    pk_cols = insp.get_pk_constraint("transcripts")["constrained_columns"]
    assert pk_cols == ["id"], pk_cols
    # SQLAlchemy 的 SQLite 方言 get_indexes 不含内联 UNIQUE 生成的 sqlite_autoindex_*；
    # 业务键唯一性改用 get_unique_constraints 验证（唯一性本身已由上面的重复键拒绝证明）。
    uc_cols = {tuple(uc["column_names"]) for uc in insp.get_unique_constraints("transcripts")}
    assert ("key",) in uc_cols, uc_cols
    print("[mig] transcripts 加自增主键 id、业务键转唯一约束 ok")

    # video_infos 同模式对齐：重建加 id、payload 保留、repo 经业务键仍能命中（未过期）
    _make_legacy_video_infos()
    actions = migrations.ensure_schema()
    assert any("video_infos: rebuilt" in a for a in actions), actions
    with storage.session() as s:
        row = s.scalar(select(models.VideoInfo).where(models.VideoInfo.key == "v" * 64))
        assert row is not None and row.id == 1, row
        assert row.payload["title"] == "旧信息"
    insp2 = __import__("sqlalchemy").inspect(storage.migrations.engine)  # 重建后重新反射，避开缓存
    assert insp2.get_pk_constraint("video_infos")["constrained_columns"] == ["id"]
    uc_cols = {tuple(uc["column_names"]) for uc in insp2.get_unique_constraints("video_infos")}
    assert ("key",) in uc_cols, uc_cols
    from backend.storage import repo

    assert repo.get_info("v" * 64)["title"] == "旧信息"   # _by_key 读路径命中
    print("[mig] video_infos 加自增主键 id、业务键转唯一约束、repo 命中 ok")


def test_migration_is_idempotent():
    from backend.storage import migrations

    assert migrations.ensure_schema() == [], "二次执行不应再有任何动作"
    assert migrations.APPLIED == []
    print("[mig] idempotent ok（二次执行无动作）")


def _cleanup():
    try:
        from backend.storage import db as sdb
        sdb.engine.dispose()
    except Exception:
        pass
    try:
        import shutil
        shutil.rmtree(_TMP_DIR, ignore_errors=True)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        test_legacy_shape_before_migration()
        test_ensure_schema_aligns_structure()
        test_data_preserved_and_fixup_applied()
        test_multiple_null_emails_coexist()
        test_ai_artifact_merge_from_legacy_tables()
        test_surrogate_id_migration()
        test_migration_is_idempotent()
        print("MIGRATION TEST PASSED")
    finally:
        _cleanup()
