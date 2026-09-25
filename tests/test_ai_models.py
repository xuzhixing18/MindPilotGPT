"""「用户自定义一键 AI 分析模型」的离线测试（含阶段3 模型目录 DB 化）。

覆盖：
- ai.config.load_config：用户覆盖参数优先级、所选服务商未配 Key 回退全局 env、纯 env 行为不变
- ai.catalog：出厂种子导入幂等（只补缺不覆盖）、进程内缓存与失效、is_selectable
- ai.config.models_payload：目录改读 ai_models 表 / 服务商可用性 / 全局默认 / 当前选择
- auth.service.update_ai_settings：同设同清、白名单、目录内已上架模型才可选
- 路由：GET /api/ai/models 开放访问；PATCH /api/me/ai-settings 匿名 401、登录后设置/清除
- 管理端点 /api/admin/ai/models：令牌门禁、CRUD、下架即时生效（设置拒绝 + 运行时回退）
- 缓存键：summary/mindmap key 随 model_label 变化（切模型自动换缓存条目，零迁移）

运行：python tests/test_ai_models.py
"""
import os
import sys
import tempfile
from pathlib import Path

# 将项目根目录加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 关键：db.py 在「导入时」即按 DATABASE_URL 建 engine，必须在导入任何 backend.* 之前设置临时库。
_TMP_DIR = Path(tempfile.mkdtemp(prefix="mp_aimodel_test_"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP_DIR / 'test_aimodel.db').as_posix()}"
os.environ["AUTH_ENABLED"] = "true"
os.environ["AUTH_ALLOW_ANONYMOUS"] = "false"

from backend import storage  # noqa: E402
from backend.ai import config as ai_config  # noqa: E402
from backend.ai.config import load_config, models_payload  # noqa: E402
from backend.auth import service, throttle  # noqa: E402
from backend.auth.errors import ValidationError  # noqa: E402
from backend.storage import keys  # noqa: E402

# AI 相关环境变量全集（.env 已在 import 时载入，测试内显式覆盖/删除以隔离真实密钥）
_AI_ENV_KEYS = (
    "AI_PROVIDER", "AI_API_KEY", "AI_MODEL", "AI_BASE_URL",
    "LLM_PROVIDER", "LLM_MODEL_ID", "LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL",
    "DEEPSEEK_API_KEY",
    "ZHIPU_API_KEY", "GLM_API_KEY", "BIGMODEL_API_KEY",
    "DASHSCOPE_API_KEY", "QWEN_API_KEY",
    "MOONSHOT_API_KEY", "KIMI_API_KEY",
    "OPENAI_API_KEY",
    # 各家专属端点/默认模型：从 PROVIDERS 派生，确保真实 .env 的 KIMI_BASE_URL /
    # QWEN_MODEL_ID 等不泄漏进用例（否则默认模型/端点断言会随本机 .env 漂移）
    *(k for preset in ai_config.PROVIDERS.values()
      for k in (*preset.get("base_url_envs", ()), *preset.get("model_envs", ()))),
)
_SAVED: dict[str, str | None] = {}


def _clear_ai_env() -> None:
    """清空 AI 配置相关环境变量（记录原值，测试结束恢复）。"""
    for key in _AI_ENV_KEYS:
        _SAVED.setdefault(key, os.environ.get(key))
        os.environ.pop(key, None)


def _restore_ai_env() -> None:
    for key, value in _SAVED.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    _SAVED.clear()


def _setup_fake_deepseek() -> None:
    """配置一个假的 deepseek Key，使 load_config() 返回非 None（其余服务商无 Key）。"""
    _clear_ai_env()
    os.environ["DEEPSEEK_API_KEY"] = "sk-ds-test"


def test_init_db():
    assert storage.init_db() is True
    print("[db] init_db ok（users 表含 ai_provider/ai_model 列）")


def test_load_config_override_and_fallback():
    _setup_fake_deepseek()
    try:
        # 纯 env：默认 deepseek + 预设默认模型（与改造前行为一致）
        cfg = load_config()
        assert cfg is not None and cfg.provider == "deepseek"
        assert cfg.model == "deepseek-v4-pro"

        # 用户覆盖：provider + model 优先于 env
        cfg = load_config(provider="deepseek", model="deepseek-flash")
        assert cfg is not None and cfg.provider == "deepseek"
        assert cfg.model == "deepseek-flash"

        # 所选服务商未配 Key（qwen/kimi 的专属 Key 已清）→ 回退全局 env
        for provider in ("qwen", "kimi"):
            cfg = load_config(provider=provider, model="whatever")
            assert cfg is not None and cfg.provider == "deepseek", f"{provider} 应回退全局 env"

        # 未知 provider（不属于平台 Key 池）→ 回退全局 env
        cfg = load_config(provider="bogus", model="x")
        assert cfg is not None and cfg.provider == "deepseek"

        # 全局也无 Key → None（AI 不可用，端点返回 503 引导配置）
        os.environ.pop("DEEPSEEK_API_KEY", None)
        assert load_config() is None
        assert load_config(provider="deepseek", model="deepseek-chat") is None
    finally:
        _restore_ai_env()
    print("[config] load_config override priority & fallback ok")


def test_models_payload_shape():
    _setup_fake_deepseek()
    try:
        data = models_payload()
        assert data["default"]["provider"] == "deepseek"
        assert data["default"]["model"] == "deepseek-v4-pro"
        assert data["current"] is None  # 未登录/未设置

        providers = {p["key"]: p for p in data["providers"]}
        assert set(providers) == set(ai_config.PROVIDERS)
        assert providers["deepseek"]["available"] is True
        assert providers["qwen"]["available"] is False   # 专属 Key 已清
        for p in data["providers"]:
            assert "label" in p and "models" in p and isinstance(p["models"], list)
            for m in p["models"]:
                assert "id" in m and "label" in m

        # 种子 label 经「种子→DB→payload」原样保留（空 label 才回退 id 大写，见管理端用例）。
        # 期望值直接从 MODEL_CATALOG 派生：运营改展示名（含大小写）时测试不随之漂移。
        for pid, seed_models in ai_config.MODEL_CATALOG.items():
            if pid not in providers:
                continue
            got = {m["id"]: m["label"] for m in providers[pid]["models"]}
            for sm in seed_models:
                assert got.get(sm["id"]) == (sm.get("label") or sm["id"].upper()), (pid, sm["id"])

        # 登录且已设置时回传 current
        data = models_payload(("deepseek", "deepseek-flash"))
        assert data["current"] == {"provider": "deepseek", "model": "deepseek-flash"}
    finally:
        _restore_ai_env()
    print("[config] models_payload shape / availability / current ok")


def test_update_ai_settings_validation():
    user = service.register("aimodel@example.com", "password123", "AI")
    uid = user["id"]

    # 同设同清：只给一边 → ValidationError（防「只设 provider 不设 model」的半状态）
    for provider, model in (("deepseek", None), (None, "deepseek-chat")):
        try:
            service.update_ai_settings(uid, provider, model)
            assert False, "半状态应抛 ValidationError"
        except ValidationError:
            pass
    # 服务商白名单：平台 Key 池之外 → ValidationError
    try:
        service.update_ai_settings(uid, "bogus", "x")
        assert False, "未知服务商应抛 ValidationError"
    except ValidationError:
        pass
    # 模型为空 / 超长
    try:
        service.update_ai_settings(uid, "deepseek", "")
        assert False, "空模型应抛 ValidationError"
    except ValidationError:
        pass
    try:
        service.update_ai_settings(uid, "deepseek", "x" * 129)
        assert False, "超长模型应抛 ValidationError"
    except ValidationError:
        pass

    # 正常设置 → 回传含两字段
    updated = service.update_ai_settings(uid, "deepseek", "deepseek-flash")
    assert updated["ai_provider"] == "deepseek"
    assert updated["ai_model"] == "deepseek-flash"

    # 清空（null/null）→ 恢复跟随平台默认
    updated = service.update_ai_settings(uid, None, None)
    assert updated["ai_provider"] is None and updated["ai_model"] is None
    print("[service] update_ai_settings validation / set / clear ok")


def test_http_ai_settings_flow():
    from fastapi.testclient import TestClient

    from backend.main import app

    _setup_fake_deepseek()
    throttle.login_limiter.reset()
    try:
        with TestClient(app) as client:
            # 目录开放访问（匿名可渲染弹窗；不含任何密钥信息）
            r = client.get("/api/ai/models")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body.get("current") is None
            assert body.get("providers")

            # 保存需登录：匿名 401
            r = client.patch("/api/auth/me/ai-settings",
                             json={"provider": "deepseek", "model": "deepseek-flash"})
            assert r.status_code == 401

            # 注册 + 登录 → 设置默认模型
            email = "aiset@example.com"
            assert client.post("/api/auth/register",
                               json={"email": email, "password": "password123"}).status_code == 201
            r = client.post("/api/auth/login", json={"email": email, "password": "password123"})
            assert r.status_code == 200, r.text

            r = client.patch("/api/auth/me/ai-settings",
                             json={"provider": "deepseek", "model": "deepseek-flash"})
            assert r.status_code == 200, r.text
            assert r.json()["user"]["ai_provider"] == "deepseek"

            # 目录回传 current（登录态）
            cur = client.get("/api/ai/models").json().get("current") or {}
            assert cur.get("provider") == "deepseek"
            assert cur.get("model") == "deepseek-flash"

            # 半状态 400
            assert client.patch("/api/auth/me/ai-settings",
                                json={"provider": "deepseek"}).status_code == 400

            # 清空 → current 回到 None（跟随平台默认）
            r = client.patch("/api/auth/me/ai-settings", json={"provider": None, "model": None})
            assert r.status_code == 200, r.text
            assert client.get("/api/ai/models").json().get("current") is None
    finally:
        _restore_ai_env()
        throttle.login_limiter.reset()
    print("[http] GET /api/ai/models + PATCH /api/me/ai-settings flow ok")


def test_cache_key_follows_model():
    """切模型 → 缓存键随 model_label 变化（换模型自然产生新缓存条目，零迁移）。"""
    base_summary = keys.summary_key("同一条字幕文本", "DeepSeek · deepseek-chat", "v1")
    other_summary = keys.summary_key("同一条字幕文本", "DeepSeek · deepseek-reasoner", "v1")
    assert base_summary != other_summary

    base_map = keys.mindmap_key("同一条字幕文本", "DeepSeek · deepseek-chat", "v1")
    other_map = keys.mindmap_key("同一条字幕文本", "DeepSeek · deepseek-reasoner", "v1")
    assert base_map != other_map

    # 同模型同文本稳定可复用（A 用户切模型不影响 B 用户命中旧模型缓存）
    assert keys.summary_key("同一条字幕文本", "DeepSeek · deepseek-chat", "v1") == base_summary
    print("[keys] cache key follows model_label ok")


# --------------------------------------------------------------------------- #
# 阶段3：模型目录 DB 化（ai_models 表 + ai.catalog）
# --------------------------------------------------------------------------- #
def test_catalog_seed_and_cache():
    from backend.ai import catalog

    # 首次导入：补齐全部出厂种子
    total_seed = sum(len(v) for v in ai_config.MODEL_CATALOG.values())
    inserted = catalog.ensure_seeded()
    rows = catalog.list_all()
    assert len(rows) >= total_seed
    assert inserted <= total_seed

    # 幂等：再导一次不重复（只补缺）
    assert catalog.ensure_seeded() == 0
    assert len(catalog.list_all()) == len(rows)

    # 按服务商分组只含已上架条目，字段结构与阶段1 兼容（id/label/tier）
    grouped = catalog.list_enabled_grouped()
    assert set(grouped) <= set(ai_config.PROVIDERS)
    assert {m["id"] for m in grouped["deepseek"]} >= {"deepseek-flash", "deepseek-v4-pro"}
    for models in grouped.values():
        for m in models:
            assert "id" in m and "label" in m and "tier" in m

    # 缓存失效后重读仍一致（写路径依赖 invalidate 生效）
    catalog.invalidate()
    assert len(catalog.list_all()) == len(rows)
    assert catalog.is_selectable("deepseek", "deepseek-flash")
    assert not catalog.is_selectable("deepseek", "no-such-model")

    # 种子 label 经「种子→DB→分组」原样保留（空 label 才回退 id 大写）。
    # 期望值直接从 MODEL_CATALOG 派生：运营改展示名（含大小写）时测试不随之漂移。
    for pid, seed_models in ai_config.MODEL_CATALOG.items():
        if pid not in grouped:
            continue
        got = {m["id"]: m["label"] for m in grouped[pid]}
        for sm in seed_models:
            assert got.get(sm["id"]) == (sm.get("label") or sm["id"].upper()), (pid, sm["id"])
    print(f"[catalog] seed idempotent / grouping / cache ok（{len(rows)} 条）")


def test_admin_endpoints_and_runtime_fallback():
    """管理端点全链路：令牌门禁 → CRUD → 下架即时生效（设置拒绝 + 运行时回退）。"""
    from fastapi.testclient import TestClient

    from backend.ai import catalog
    from backend.auth.dependencies import CurrentUser, ai_override_cfg
    from backend.main import app

    os.environ["ADMIN_API_TOKEN"] = "tok-4-test"
    catalog.invalidate()
    try:
        with TestClient(app) as client:
            headers = {"Authorization": "Bearer tok-4-test"}

            # 门禁：未带令牌 401；令牌错误 401
            assert client.get("/api/admin/ai/models").status_code == 401
            assert client.get("/api/admin/ai/models",
                               headers={"Authorization": "Bearer wrong"}).status_code == 401

            # 列表：含种子条目与计费倍率字段
            r = client.get("/api/admin/ai/models", headers=headers)
            assert r.status_code == 200, r.text
            all_models = r.json()["models"]
            assert len(all_models) >= len(ai_config.MODEL_CATALOG["deepseek"])
            assert all("price_multiplier" in m and "enabled" in m for m in all_models)

            # 新增：未知服务商 / 重复条目 → 400
            payload = {"provider": "deepseek", "model": "deepseek-test-x",
                       "label": "测试模型", "tier": "$$"}
            assert client.post("/api/admin/ai/models", headers=headers,
                               json={"provider": "bogus", "model": "x"}).status_code == 400
            r = client.post("/api/admin/ai/models", headers=headers, json=payload)
            assert r.status_code == 201, r.text
            mid = r.json()["model"]["id"]
            assert client.post("/api/admin/ai/models", headers=headers,
                               json=payload).status_code == 400

            # 用户可选新上架的模型
            user = service.register("adm@example.com", "password123", "A")
            updated = service.update_ai_settings(user["id"], "deepseek", "deepseek-test-x")
            assert updated["ai_model"] == "deepseek-test-x"

            # 弹窗目录出现新模型
            visible = {m["id"] for p in client.get("/api/ai/models").json()["providers"]
                       for m in p["models"]}
            assert "deepseek-test-x" in visible

            # label 省略（空）：管理端存空，用户侧 payload 回退大写 ID
            r = client.post("/api/admin/ai/models", headers=headers,
                            json={"provider": "deepseek", "model": "deepseek-test-y", "tier": "$"})
            assert r.status_code == 201, r.text
            assert r.json()["model"]["label"] == ""  # 管理端可见真实值（未指定）
            yid = r.json()["model"]["id"]
            visible = {m["id"]: m["label"] for p in client.get("/api/ai/models").json()["providers"]
                       for m in p["models"]}
            assert visible["deepseek-test-y"] == "DEEPSEEK-TEST-Y"

            # PATCH 指定 label → 用指定值；再清空 → 恢复大写回退
            assert client.patch(f"/api/admin/ai/models/{yid}", headers=headers,
                                json={"label": "Y 测试"}).status_code == 200
            visible = {m["id"]: m["label"] for p in client.get("/api/ai/models").json()["providers"]
                       for m in p["models"]}
            assert visible["deepseek-test-y"] == "Y 测试"
            assert client.patch(f"/api/admin/ai/models/{yid}", headers=headers,
                                json={"label": ""}).status_code == 200
            visible = {m["id"]: m["label"] for p in client.get("/api/ai/models").json()["providers"]
                       for m in p["models"]}
            assert visible["deepseek-test-y"] == "DEEPSEEK-TEST-Y"

            # 下架：设置被拒 + 运行时回退全局默认 + 弹窗不再展示
            r = client.patch(f"/api/admin/ai/models/{mid}", headers=headers,
                             json={"enabled": False})
            assert r.status_code == 200 and r.json()["model"]["enabled"] is False
            try:
                service.update_ai_settings(user["id"], "deepseek", "deepseek-test-x")
                assert False, "已下架模型应被拒绝"
            except ValidationError:
                pass
            u = CurrentUser(user_id=user["id"], ai_provider="deepseek",
                            ai_model="deepseek-test-x", is_authenticated=True)
            assert ai_override_cfg(u) is None  # 下架 → 回退全局 env
            visible = {m["id"] for p in client.get("/api/ai/models").json()["providers"]
                       for m in p["models"]}
            assert "deepseek-test-x" not in visible

            # 管理列表仍含已下架条目
            assert any(m["id"] == mid for m in
                       client.get("/api/admin/ai/models", headers=headers).json()["models"])

            # 删除：二次删 → 404；改不存在的条目 → 404；非法字段 → 400
            assert client.delete(f"/api/admin/ai/models/{mid}", headers=headers).status_code == 200
            assert client.delete(f"/api/admin/ai/models/{mid}", headers=headers).status_code == 404
            assert client.delete(f"/api/admin/ai/models/{yid}", headers=headers).status_code == 200
            assert client.patch("/api/admin/ai/models/nope", headers=headers,
                                json={"label": "x"}).status_code == 404
            seed_id = next(m["id"] for m in all_models if m["model"] == "deepseek-flash")
            assert client.patch(f"/api/admin/ai/models/{seed_id}", headers=headers,
                                json={"provider": "qwen"}).status_code == 400  # 不可改 provider

            # reseed 只补缺：被删的自建条目不复活，种子条目不重复
            r = client.post("/api/admin/ai/models/reseed", headers=headers)
            assert r.status_code == 200
            names = [m["model"] for m in
                     client.get("/api/admin/ai/models", headers=headers).json()["models"]
                     if m["provider"] == "deepseek"]
            assert names.count("deepseek-flash") == 1 and "deepseek-test-x" not in names
    finally:
        os.environ.pop("ADMIN_API_TOKEN", None)
        catalog.invalidate()
    print("[admin] token gate / CRUD / delist runtime fallback ok")


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
        test_init_db()
        test_load_config_override_and_fallback()
        test_models_payload_shape()
        test_update_ai_settings_validation()
        test_http_ai_settings_flow()
        test_cache_key_follows_model()
        test_catalog_seed_and_cache()
        test_admin_endpoints_and_runtime_fallback()
    finally:
        _cleanup()
