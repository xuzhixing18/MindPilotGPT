"""思维导图 / AI 问答能力的离线测试（不触网、不需真实 Key）。

覆盖：
- mindmap ：LLM 回复的 JSON 提取容错、树根定位（root/mindmap/tree/data 包裹或数组）、
            节点规整（别名 title/name/text/label、children/nodes/sub/items/child、
            超深/超宽/空节点裁剪）、mindmap_key 敏感性与键空间隔离、
            repo get/put_mindmap 往返、build_mindmap 缓存命中/refresh/single-flight、
            空文本与未配置的语义化异常
- qa      ：_sanitize_history 清洗（角色/长度/条数/非 list）、ask 的 messages 结构
            （system 含字幕 + 历史 + 当前问题）、空字幕/空问题/未配置/空回复/LLMError 映射

关键：db.py 在「导入时」即按 DATABASE_URL 建 engine，故必须在导入任何 backend.* 之前，
把 DATABASE_URL 指向临时库；并用 monkeypatch 替换 llm.chat 避免真实网络调用。

运行：python tests/test_ai_mindmap_qa.py
"""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

# 将项目根目录加入 sys.path，保证从任意位置运行都能导入 backend 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 关键：在导入任何 backend.* 之前，把 DATABASE_URL 指向临时库，避免污染 data/mindpilot.db
_TMP_DIR = Path(tempfile.mkdtemp(prefix="mp_mindmap_qa_test_"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP_DIR / 'test_mm_qa.db').as_posix()}"
os.environ["CACHE_ENABLED"] = "true"

from backend import storage  # noqa: E402
from backend.ai import config as ai_config  # noqa: E402
from backend.ai import llm as llm_mod  # noqa: E402
from backend.ai import mindmap as ai_mindmap  # noqa: E402
from backend.ai import qa as ai_qa  # noqa: E402
from backend.storage import db as sdb  # noqa: E402
from backend.storage import keys, repo  # noqa: E402

# _set_fake_llm 钉住 AI_MODEL=deepseek-chat，故 model_label（label · model）恒为下面这个串
_MODEL_LABEL = "DeepSeek · deepseek-chat"

_AI_ENV_KEYS = (
    "AI_PROVIDER", "AI_API_KEY", "AI_MODEL", "AI_BASE_URL",
    "LLM_PROVIDER", "LLM_MODEL_ID", "LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL",
    "DEEPSEEK_API_KEY",
    "ZHIPU_API_KEY", "GLM_API_KEY", "BIGMODEL_API_KEY",
    "DASHSCOPE_API_KEY", "QWEN_API_KEY",
    "MOONSHOT_API_KEY", "KIMI_API_KEY",
    "OPENAI_API_KEY",
    # 各家专属端点/默认模型：从 PROVIDERS 派生，隔离真实 .env（本文件另钉住 AI_MODEL）
    *(k for preset in ai_config.PROVIDERS.values()
      for k in (*preset.get("base_url_envs", ()), *preset.get("model_envs", ()))),
)


def _clear_ai_env():
    for key in _AI_ENV_KEYS:
        os.environ.pop(key, None)


def _set_fake_llm():
    """配置一个假的 deepseek，使 load_config() 返回非 None（label/model 见 _MODEL_LABEL）。

    显式钉住 AI_MODEL=deepseek-chat：model_label 由「label · model」组成，钉住模型可令
    _MODEL_LABEL 恒定，不随 config 预设默认模型（如 deepseek-v4-pro）变更而漂移。
    """
    _clear_ai_env()
    os.environ["AI_PROVIDER"] = "deepseek"
    os.environ["AI_API_KEY"] = "sk-test"
    os.environ["AI_MODEL"] = "deepseek-chat"


def _patch_chat(fn):
    """monkeypatch backend.ai.llm.chat，返回原函数以便还原。"""
    orig = llm_mod.chat
    llm_mod.chat = fn
    return orig


# --------------------------------------------------------------------------- #
# mindmap：解析与规整
# --------------------------------------------------------------------------- #
def test_mindmap_extract_json():
    assert ai_mindmap._extract_json('{"title":"a","children":[]}')["title"] == "a"
    assert ai_mindmap._extract_json('```json\n{"title":"x"}\n```')["title"] == "x"
    assert ai_mindmap._extract_json('```{"title":"y"}```')["title"] == "y"  # 无 json 标识的围栏
    assert ai_mindmap._extract_json('好的，结果：{"title":"z"} 希望')["title"] == "z"
    assert ai_mindmap._extract_json('[{"title":"m"}]') == [{"title": "m"}]  # 根为数组
    try:
        ai_mindmap._extract_json("完全没有 JSON 内容")
        assert False, "应抛 MindmapError"
    except ai_mindmap.MindmapError:
        pass
    print("[mindmap] extract_json ok")


def test_mindmap_find_root():
    # 直接数组 -> 包成 {title:"", children:[...]}
    assert ai_mindmap._find_root([{"title": "a"}]) == {"title": "", "children": [{"title": "a"}]}
    # root 包裹
    assert ai_mindmap._find_root({"root": {"title": "中心", "children": []}})["title"] == "中心"
    # 多层包裹 mindmap -> data
    nested = ai_mindmap._find_root({"mindmap": {"data": {"title": "T", "children": []}}})
    assert nested["title"] == "T"
    # 普通 dict（无包裹键）原样返回
    d = {"title": "X", "children": []}
    assert ai_mindmap._find_root(d) is d
    # 标量 -> 空树
    assert ai_mindmap._find_root("字符串") == {"title": "", "children": []}
    assert ai_mindmap._find_root(42) == {"title": "", "children": []}
    print("[mindmap] find_root ok")


def test_mindmap_normalize_node():
    # 字符串节点
    assert ai_mindmap._normalize_node("  叶子  ") == {"title": "叶子", "children": []}
    assert ai_mindmap._normalize_node("   ") is None  # 空字符串 -> None
    # 标题别名 name/text/label
    assert ai_mindmap._normalize_node({"name": "A"})["title"] == "A"
    assert ai_mindmap._normalize_node({"text": "B"})["title"] == "B"
    assert ai_mindmap._normalize_node({"label": "C"})["title"] == "C"
    # children 别名 nodes/items
    assert ai_mindmap._normalize_node({"title": "R", "nodes": [{"title": "n"}]})["children"][0]["title"] == "n"
    assert ai_mindmap._normalize_node({"title": "R", "items": ["x", "y"]})["children"] == [
        {"title": "x", "children": []}, {"title": "y", "children": []},
    ]
    # 空节点（无 title 无 children）-> None；非 dict/str -> None
    assert ai_mindmap._normalize_node({}) is None
    assert ai_mindmap._normalize_node({"children": []}) is None
    assert ai_mindmap._normalize_node(123) is None
    assert ai_mindmap._normalize_node(None) is None

    # 深度裁剪：超过 _MAX_DEPTH 的层被丢弃
    deep = {"title": "l0"}
    node = deep
    for i in range(1, 8):
        node["children"] = [{"title": f"l{i}"}]
        node = node["children"][0]
    root = ai_mindmap._normalize_node(deep)
    depth, cur = 0, root
    while cur["children"]:
        depth += 1
        cur = cur["children"][0]
    assert depth == ai_mindmap._MAX_DEPTH, f"深度应裁剪到 {ai_mindmap._MAX_DEPTH}，实际 {depth}"

    # 宽度裁剪：超过 _MAX_CHILDREN 被截断
    wide = {"title": "R", "children": [{"title": f"c{i}"} for i in range(20)]}
    assert len(ai_mindmap._normalize_node(wide)["children"]) == ai_mindmap._MAX_CHILDREN
    print("[mindmap] normalize_node ok")


def test_mindmap_key():
    base = keys.mindmap_key("text", "modelA", "v1")
    assert base == keys.mindmap_key("text", "modelA", "v1")  # 稳定可复现
    variants = {
        base,
        keys.mindmap_key("text", "modelB", "v1"),   # 换模型
        keys.mindmap_key("text", "modelA", "v2"),   # 换提示词版本
        keys.mindmap_key("text2", "modelA", "v1"),  # 换文本
    }
    assert len(variants) == 4  # 任一维度变化即不同 key
    # 与 summary_key 键空间隔离（相同入参也应不同 key，因加了 mindmap 前缀）
    assert base != keys.summary_key("text", "modelA", "v1")
    print("[keys] mindmap_key sensitivity & isolation ok")


def test_repo_mindmap_roundtrip():
    key = keys.mindmap_key("导图往返文本", _MODEL_LABEL, "v1")
    mm = {
        "title": "中心", "children": [{"title": "分支", "children": []}],
        "model": _MODEL_LABEL, "truncated": False, "cached": False,
    }
    repo.put_mindmap(key, mm, model=_MODEL_LABEL, prompt_version="v1", title="标题")
    got = repo.get_mindmap(key)
    assert got is not None
    assert got["title"] == "中心"
    assert got["children"] == [{"title": "分支", "children": []}]
    assert "cached" not in got  # put 时剔除运行时标记
    assert repo.get_mindmap(keys.mindmap_key("不存在的文本", "m", "v1")) is None  # 未命中
    print("[repo] mindmap roundtrip ok")


# --------------------------------------------------------------------------- #
# mindmap：build_mindmap（异常 / 缓存 / 并发去重）
# --------------------------------------------------------------------------- #
def test_build_mindmap_errors():
    _clear_ai_env()
    # 空字幕 -> MindmapError（早于配置检查）
    try:
        ai_mindmap.build_mindmap("   ", "标题")
        assert False, "空文本应抛 MindmapError"
    except ai_mindmap.MindmapError:
        pass
    # 无配置 -> AINotConfiguredError
    try:
        ai_mindmap.build_mindmap("一些字幕", "标题")
        assert False, "无 Key 应抛 AINotConfiguredError"
    except ai_mindmap.AINotConfiguredError:
        pass
    # LLMError -> MindmapError
    _set_fake_llm()

    def _boom(cfg, messages, **kwargs):
        raise llm_mod.LLMError("上游超时")

    orig = _patch_chat(_boom)
    try:
        ai_mindmap.build_mindmap("会触发上游错误的字幕", "标题")
        assert False, "LLMError 应映射为 MindmapError"
    except ai_mindmap.MindmapError:
        pass
    finally:
        llm_mod.chat = orig
    print("[mindmap] empty-text / no-key / LLMError ok")


def test_build_mindmap_cache():
    _set_fake_llm()
    reply = json.dumps({
        "title": "中心主题",
        "children": [
            {"title": "分支A", "children": [{"title": "要点A1", "children": []}]},
            {"title": "分支B", "children": []},
        ],
    }, ensure_ascii=False)
    calls = {"n": 0}

    def fake_chat(cfg, messages, **kwargs):
        calls["n"] += 1
        return reply

    orig = _patch_chat(fake_chat)
    try:
        text = "这是用于思维导图缓存测试的字幕文本。"
        r1 = ai_mindmap.build_mindmap(text, "标题")
        assert r1["cached"] is False and calls["n"] == 1
        assert r1["title"] == "中心主题"
        assert r1["model"] == _MODEL_LABEL
        assert r1["truncated"] is False
        assert len(r1["children"]) == 2
        assert r1["children"][0]["children"][0]["title"] == "要点A1"

        r2 = ai_mindmap.build_mindmap(text, "标题")
        assert r2["cached"] is True and calls["n"] == 1  # 命中缓存，不再调用 LLM
        assert r2["title"] == "中心主题"

        r3 = ai_mindmap.build_mindmap(text, "标题", refresh=True)
        assert r3["cached"] is False and calls["n"] == 2  # refresh 强制重算并覆盖

        r4 = ai_mindmap.build_mindmap(text, "标题")
        assert r4["cached"] is True and calls["n"] == 2  # 重算后再次命中
    finally:
        llm_mod.chat = orig
    print("[mindmap] build_mindmap cache hit / refresh ok")


def test_build_mindmap_singleflight():
    _set_fake_llm()
    reply = json.dumps({"title": "并发中心", "children": []}, ensure_ascii=False)
    calls = {"n": 0}
    barrier = threading.Barrier(2)

    def fake_chat(cfg, messages, **kwargs):
        calls["n"] += 1
        time.sleep(0.05)  # 放大竞态窗口
        return reply

    orig = _patch_chat(fake_chat)
    results: dict = {}

    def worker(i):
        barrier.wait()  # 两线程尽量同时进入
        results[i] = ai_mindmap.build_mindmap("并发去重专用文本", "标题")

    try:
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # single-flight + 双重检查：并发同 key 只真正调用一次 LLM
        assert calls["n"] == 1, f"single-flight 应只调一次 LLM，实际 {calls['n']}"
        assert results[0]["title"] == "并发中心" and results[1]["title"] == "并发中心"
    finally:
        llm_mod.chat = orig
    print("[mindmap] single-flight concurrent computed once ok")


# --------------------------------------------------------------------------- #
# qa：历史清洗 / messages 结构 / 异常
# --------------------------------------------------------------------------- #
def test_qa_sanitize_history():
    h = ai_qa._sanitize_history([
        {"role": "user", "content": "  问题1  "},
        {"role": "assistant", "content": "回答1"},
        {"role": "system", "content": "应被过滤"},   # 非法角色
        {"role": "user", "content": "   "},          # 空内容
        "非字典项",                                    # 非字典
        {"role": "assistant", "content": None},       # None 内容
    ])
    assert h == [
        {"role": "user", "content": "问题1"},
        {"role": "assistant", "content": "回答1"},
    ]
    # 非 list -> []
    assert ai_qa._sanitize_history(None) == []
    assert ai_qa._sanitize_history("x") == []
    # 超过上限只保留最后 N 条
    long = [{"role": "user", "content": f"q{i}"} for i in range(20)]
    kept = ai_qa._sanitize_history(long)
    assert len(kept) == ai_qa._MAX_HISTORY_MESSAGES
    assert kept[-1]["content"] == "q19"  # 保留的是最新的
    # 单条超长被截断
    big = ai_qa._sanitize_history([{"role": "user", "content": "x" * 5000}])
    assert len(big[0]["content"]) == ai_qa._MAX_HISTORY_ITEM_CHARS
    print("[qa] sanitize_history ok")


def test_qa_ask_messages():
    _set_fake_llm()
    captured: dict = {}

    def fake_chat(cfg, messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return "这是基于字幕的回答。"

    orig = _patch_chat(fake_chat)
    try:
        result = ai_qa.ask(
            "字幕内容：介绍了三个要点。",
            "视频标题",
            "第二个要点是什么？",
            history=[
                {"role": "user", "content": "第一个要点是什么？"},
                {"role": "assistant", "content": "第一个要点是……"},
            ],
        )
        assert result["answer"] == "这是基于字幕的回答。"
        assert result["question"] == "第二个要点是什么？"
        assert result["model"] == _MODEL_LABEL

        msgs = captured["messages"]
        # 结构：system(含字幕) + 2 条历史 + 当前问题 = 4 条
        assert len(msgs) == 4
        assert msgs[0]["role"] == "system"
        assert "字幕内容：介绍了三个要点。" in msgs[0]["content"]  # 字幕注入系统提示
        assert msgs[1] == {"role": "user", "content": "第一个要点是什么？"}
        assert msgs[2]["role"] == "assistant"
        assert msgs[-1] == {"role": "user", "content": "第二个要点是什么？"}
    finally:
        llm_mod.chat = orig
    print("[qa] ask messages structure ok")


def test_qa_errors():
    _clear_ai_env()
    # 空字幕 -> QAError（早于配置检查）
    try:
        ai_qa.ask("   ", "标题", "问题")
        assert False, "空字幕应抛 QAError"
    except ai_qa.QAError:
        pass
    # 空问题 -> QAError（早于配置检查）
    try:
        ai_qa.ask("一些字幕", "标题", "   ")
        assert False, "空问题应抛 QAError"
    except ai_qa.QAError:
        pass
    # 有字幕+问题但无配置 -> AINotConfiguredError
    try:
        ai_qa.ask("一些字幕", "标题", "问题")
        assert False, "无 Key 应抛 AINotConfiguredError"
    except ai_qa.AINotConfiguredError:
        pass

    _set_fake_llm()
    # LLM 返回空 -> QAError
    orig = _patch_chat(lambda cfg, messages, **kwargs: "   ")
    try:
        ai_qa.ask("一些字幕", "标题", "问题")
        assert False, "空回复应抛 QAError"
    except ai_qa.QAError:
        pass
    finally:
        llm_mod.chat = orig
    # LLMError -> QAError
    def _boom(cfg, messages, **kwargs):
        raise llm_mod.LLMError("上游限流")

    orig = _patch_chat(_boom)
    try:
        ai_qa.ask("一些字幕", "标题", "问题")
        assert False, "LLMError 应映射为 QAError"
    except ai_qa.QAError:
        pass
    finally:
        llm_mod.chat = orig
    print("[qa] empty-text / empty-question / no-key / empty-answer / LLMError ok")


def _cleanup():
    """尽力释放引擎并删除临时库（Windows 下 WAL 文件可能短暂占用，失败即忽略）。"""
    try:
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
        assert storage.init_db() is True
        test_mindmap_extract_json()
        test_mindmap_find_root()
        test_mindmap_normalize_node()
        test_mindmap_key()
        test_repo_mindmap_roundtrip()
        test_build_mindmap_errors()
        test_build_mindmap_cache()
        test_build_mindmap_singleflight()
        test_qa_sanitize_history()
        test_qa_ask_messages()
        test_qa_errors()
        print("MINDMAP & QA TEST PASSED")
    finally:
        _cleanup()
