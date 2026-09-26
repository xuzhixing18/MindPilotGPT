"""jsonx 稳健 JSON 提取与修复的离线测试（不触网、不需真实 Key）。

覆盖：
- extract_json ：多候选提取（代码围栏 / 全文 / 字符串感知顶层平衡片段 / 首{到尾}）
- repair_json  ：补漏逗号 / 去尾随逗号的本地修复，及字符串感知（绝不改动字符串内容）
- chat_json    ：首次成功直接解析（仅一次调用）；首次失败自动模型修复重试
                 （断言重试次数与修复消息结构：坏输出原样回传 + 修复指令）

关键：db.py 在「导入时」即按 DATABASE_URL 建 engine（backend.ai 门面间接导入 storage），
故必须在导入任何 backend.* 之前把 DATABASE_URL 指向临时库；并用 monkeypatch 替换
llm.chat_meta 避免真实网络调用。

运行：python tests/test_ai_jsonx.py
"""
import os
import sys
import tempfile
from pathlib import Path

# 将项目根目录加入 sys.path，保证从任意位置运行都能导入 backend 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 关键：在导入任何 backend.* 之前，把 DATABASE_URL 指向临时库，避免污染 data/mindpilot.db
_TMP_DIR = Path(tempfile.mkdtemp(prefix="mp_jsonx_test_"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP_DIR / 'test_jsonx.db').as_posix()}"

from backend.ai import jsonx  # noqa: E402
from backend.ai import llm as llm_mod  # noqa: E402
from backend.ai.config import LLMConfig  # noqa: E402

_CFG = LLMConfig(
    provider="deepseek", label="DeepSeek", base_url="http://fake.invalid/v1",
    api_key="sk-test", model="deepseek-chat",
)


def test_extract_candidates():
    # 裸 JSON / 带围栏 / 无 json 标识围栏 / 夹带杂文本 / 根为数组
    assert jsonx.extract_json('{"a":1}') == {"a": 1}
    assert jsonx.extract_json('```json\n{"a":2}\n```') == {"a": 2}
    assert jsonx.extract_json('```{"a":3}```') == {"a": 3}
    assert jsonx.extract_json('好的，结果：{"a":4} 希望') == {"a": 4}
    assert jsonx.extract_json('[{"a":5}]') == [{"a": 5}]
    # 夹带推理杂音（自身含花括号）：字符串感知平衡片段锁定真正的顶层对象
    noisy = '推理：{"x"} 不是目标\n{"a":6}'
    assert jsonx.extract_json(noisy) == {"a": 6}
    # 完全无 JSON -> JSONExtractError（消息可直接面向用户）
    try:
        jsonx.extract_json("完全没有 JSON 内容")
        assert False, "应抛 JSONExtractError"
    except jsonx.JSONExtractError:
        pass
    print("[jsonx] candidates ok")


def test_repair_local():
    # 漏逗号：对象成员间 / 数组元素间 / 字符串值间
    assert jsonx.extract_json('{"a":1 "b":2}') == {"a": 1, "b": 2}
    assert jsonx.extract_json('{"a":[1 2]}') == {"a": [1, 2]}
    assert jsonx.extract_json('{"a":"x" "b":"y"}') == {"a": "x", "b": "y"}
    # 尾随逗号：对象 / 数组
    assert jsonx.extract_json('{"a":1,}') == {"a": 1}
    assert jsonx.extract_json('{"a":[1,2,],}') == {"a": [1, 2]}
    # 字符串感知：串内花括号 / 逗号 / 转义引号绝不被改动（修复前后逐字相同）
    raw = '{"a":"} , \\" [","b":2}'
    assert jsonx.repair_json(raw) == raw
    assert jsonx.extract_json(raw) == {"a": '} , " [', "b": 2}
    print("[jsonx] repair ok")


def test_chat_json_first_ok():
    calls = {"n": 0}

    def fake_meta(cfg, messages, **kwargs):
        calls["n"] += 1
        return {"content": '{"a":1}', "finish_reason": "stop", "usage": {}}

    orig = llm_mod.chat_meta
    llm_mod.chat_meta = fake_meta
    try:
        assert jsonx.chat_json(_CFG, [{"role": "user", "content": "hi"}]) == {"a": 1}
    finally:
        llm_mod.chat_meta = orig
    assert calls["n"] == 1, "首次解析成功不应触发修复重试"
    print("[jsonx] chat_json first-ok single call ok")


def test_chat_json_repair_retry():
    calls = {"n": 0}
    seen: dict = {}
    broken = '{"a": "未闭合'  # 原文与本地修复均无法挽救 -> 必须走模型重试

    def fake_meta(cfg, messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"content": broken, "finish_reason": "length", "usage": {}}
        seen["messages"] = messages
        return {"content": '{"a":1}', "finish_reason": "stop", "usage": {}}

    orig = llm_mod.chat_meta
    llm_mod.chat_meta = fake_meta
    try:
        assert jsonx.chat_json(_CFG, [{"role": "user", "content": "hi"}]) == {"a": 1}
    finally:
        llm_mod.chat_meta = orig
    assert calls["n"] == 2, f"应重试一次模型修复，实际 {calls['n']}"
    msgs = seen["messages"]
    assert msgs[-2] == {"role": "assistant", "content": broken}  # 坏输出原样回传
    assert "合法 JSON" in msgs[-1]["content"]  # 修复指令作为最后一条 user 消息
    print("[jsonx] chat_json repair retry ok")


if __name__ == "__main__":
    test_extract_candidates()
    test_repair_local()
    test_chat_json_first_ok()
    test_chat_json_repair_retry()
    print("JSONX TEST PASSED")
