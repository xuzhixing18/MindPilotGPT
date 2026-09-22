"""转写与 AI 总结模块的离线测试（不依赖网络与真实 API Key）。

覆盖：
- config    ：多 provider 解析、专属/通用 Key、模型与端点覆盖、自定义端点、无 Key 降级
- summarize ：LLM 返回的 JSON 提取容错、结构规整、未配置时抛 AINotConfiguredError
- subtitles ：时间戳解析、VTT/SRT 解析、自动字幕相邻去重
- asr       ：ASR provider 链解析（硅基流动/DashScope/自定义）、顺序、无 Key 降级、分段合成
- 门面编排 ：无字幕时按平台给出差异化可操作提示

运行：python tests/test_transcribe_ai.py
"""
import os
import sys
import tempfile
from pathlib import Path

# 将项目根目录加入 sys.path，保证从任意位置运行都能导入 backend 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.ai import config as ai_config  # noqa: E402
from backend.ai import summary as ai_summary  # noqa: E402
from backend.transcribe import subtitles  # noqa: E402
from backend.transcribe import asr  # noqa: E402
from backend import transcribe as tc  # noqa: E402  （门面，验证导入链无循环）

_AI_ENV_KEYS = (
    # 推荐命名
    "AI_PROVIDER", "AI_API_KEY", "AI_MODEL", "AI_BASE_URL",
    # 旧命名兼容
    "LLM_PROVIDER", "LLM_MODEL_ID", "LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL",
    # 各家专属 Key（含常见别名）
    "DEEPSEEK_API_KEY",
    "ZHIPU_API_KEY", "GLM_API_KEY", "BIGMODEL_API_KEY",
    "DASHSCOPE_API_KEY", "QWEN_API_KEY",
    "MOONSHOT_API_KEY", "KIMI_API_KEY",
    "OPENAI_API_KEY",
)


def _clear_ai_env():
    for key in _AI_ENV_KEYS:
        os.environ.pop(key, None)


_ASR_ENV_KEYS = (
    "SILICONFLOW_API_KEY", "DASHSCOPE_API_KEY", "QWEN_API_KEY",
    "ASR_PROVIDER", "ASR_API_KEY", "ASR_BASE_URL", "ASR_MODEL",
    "ASR_STYLE", "ASR_LABEL", "ASR_LANGUAGE",
    "SILICONFLOW_ASR_MODEL", "SILICONFLOW_BASE_URL",
    "DASHSCOPE_ASR_MODEL", "DASHSCOPE_BASE_URL",
)


def _clear_asr_env():
    for key in _ASR_ENV_KEYS:
        os.environ.pop(key, None)


def test_config_no_key():
    _clear_ai_env()
    assert ai_config.load_config() is None
    assert ai_config.ai_available() is False
    print("[config] no-key -> unavailable ok")


def test_config_providers():
    _clear_ai_env()
    # DeepSeek + 通用 Key
    os.environ["AI_PROVIDER"] = "deepseek"
    os.environ["AI_API_KEY"] = "sk-test"
    cfg = ai_config.load_config()
    assert cfg is not None and cfg.provider == "deepseek"
    assert cfg.base_url == "https://api.deepseek.com/v1"
    assert cfg.model == "deepseek-chat"
    assert ai_config.ai_available() is True

    # 智谱 + 专属 Key 变量名
    os.environ["AI_PROVIDER"] = "zhipu"
    os.environ.pop("AI_API_KEY")
    os.environ["ZHIPU_API_KEY"] = "z-test"
    cfg = ai_config.load_config()
    assert cfg.model == "glm-4-flash" and cfg.api_key == "z-test"

    # 覆盖默认模型
    os.environ["AI_MODEL"] = "glm-4-plus"
    assert ai_config.load_config().model == "glm-4-plus"

    # 自定义（未预设）provider：需显式 base_url + model + key
    _clear_ai_env()
    os.environ["AI_PROVIDER"] = "myllm"
    os.environ["AI_BASE_URL"] = "https://my.endpoint/v1"
    os.environ["AI_MODEL"] = "my-model"
    os.environ["AI_API_KEY"] = "k"
    cfg = ai_config.load_config()
    assert cfg.base_url == "https://my.endpoint/v1" and cfg.model == "my-model"

    # 自定义 provider 缺 base_url -> 不可用
    os.environ.pop("AI_BASE_URL")
    assert ai_config.load_config() is None
    _clear_ai_env()
    print("[config] providers/override/custom ok")


def test_config_aliases():
    _clear_ai_env()
    # provider 别名：glm -> zhipu；专属 Key 别名 GLM_API_KEY
    os.environ["AI_PROVIDER"] = "glm"
    os.environ["GLM_API_KEY"] = "g-test"
    cfg = ai_config.load_config()
    assert cfg is not None and cfg.provider == "zhipu"
    assert cfg.api_key == "g-test" and cfg.model == "glm-4-flash"

    # 旧命名 LLM_* 兼容：LLM_MODEL_ID + LLM_API_KEY
    _clear_ai_env()
    os.environ["LLM_MODEL_ID"] = "kimi"
    os.environ["LLM_API_KEY"] = "k-test"
    cfg = ai_config.load_config()
    assert cfg is not None and cfg.provider == "kimi" and cfg.api_key == "k-test"
    assert cfg.base_url == "https://api.moonshot.cn/v1"

    # AI_API_KEY 优先于专属 Key
    _clear_ai_env()
    os.environ["AI_PROVIDER"] = "deepseek"
    os.environ["AI_API_KEY"] = "generic"
    os.environ["DEEPSEEK_API_KEY"] = "specific"
    assert ai_config.load_config().api_key == "generic"

    # 模型ID前缀写法：kimi-k3 -> provider=kimi + model 覆盖为 kimi-k3
    _clear_ai_env()
    os.environ["LLM_MODEL_ID"] = "kimi-k3"
    os.environ["LLM_API_KEY"] = "k3-test"
    cfg = ai_config.load_config()
    assert cfg is not None and cfg.provider == "kimi"
    assert cfg.model == "kimi-k3" and cfg.api_key == "k3-test"
    assert cfg.base_url == "https://api.moonshot.cn/v1"

    # gpt-4o 前缀 -> openai + model 覆盖
    _clear_ai_env()
    os.environ["AI_PROVIDER"] = "gpt-4o"
    os.environ["OPENAI_API_KEY"] = "o-test"
    cfg = ai_config.load_config()
    assert cfg.provider == "openai" and cfg.model == "gpt-4o"
    _clear_ai_env()
    print("[config] aliases & LLM_* compat & model-id prefix ok")


def test_extract_json():
    assert ai_summary._extract_json('{"one_line":"a","summary":"b"}')["one_line"] == "a"
    assert ai_summary._extract_json('```json\n{"one_line":"x"}\n```')["one_line"] == "x"
    assert ai_summary._extract_json('好的：{"one_line":"y"}  hope')["one_line"] == "y"
    try:
        ai_summary._extract_json("完全没有 JSON 内容")
        assert False, "应抛 SummarizeError"
    except ai_summary.SummarizeError:
        pass
    print("[extract_json] ok")


def test_normalize():
    raw = {
        "one_line": " 一句话 ",
        "summary": "摘要",
        "key_points": ["要点1", "", "要点2"],
        "chapters": [{"title": "c1", "summary": "s1"}, "纯字符串章节", {"bad": 1}],
        "keywords": "单个关键词",
    }
    n = ai_summary._normalize(raw)
    assert n["one_line"] == "一句话"
    assert n["key_points"] == ["要点1", "要点2"]
    assert n["chapters"][0] == {"title": "c1", "summary": "s1"}
    assert n["chapters"][1] == {"title": "纯字符串章节", "summary": ""}
    assert n["chapters"][2] == {"title": "", "summary": ""}
    assert n["keywords"] == ["单个关键词"]
    print("[normalize] ok")


def test_summarize_no_key():
    _clear_ai_env()
    try:
        ai_summary.summarize("一些字幕文本", "标题")
        assert False, "应抛 AINotConfiguredError"
    except ai_summary.AINotConfiguredError:
        pass
    # 空文本应抛 SummarizeError（早于配置检查）
    try:
        ai_summary.summarize("   ", "标题")
        assert False, "空文本应抛 SummarizeError"
    except ai_summary.SummarizeError:
        pass
    print("[summarize] no-key & empty-text ok")


def test_parse_ts():
    assert subtitles._parse_ts("00:01:02.500") == 62.5
    assert subtitles._parse_ts("01:02,500") == 62.5
    assert subtitles._parse_ts("00:05.000") == 5.0
    assert subtitles._parse_ts("00:00:01.000 align:start position:0%") == 1.0
    assert subtitles._parse_ts("") == 0.0
    print("[parse_ts] ok")


def _write_tmp(content: str, suffix: str) -> Path:
    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding="utf-8") as f:
        f.write(content)
        return Path(f.name)


def test_parse_vtt():
    vtt = (
        "WEBVTT\n"
        "Kind: language\n"
        "Language: zh\n\n"
        "00:00:01.000 --> 00:00:04.000\n"
        "你好<c> 世界</c>\n\n"
        "00:00:05.000 --> 00:00:08.000 align:start position:0%\n"
        "第二行字幕\n"
    )
    path = _write_tmp(vtt, ".vtt")
    try:
        segs = subtitles._parse_subtitle_file(path)
    finally:
        path.unlink(missing_ok=True)
    assert len(segs) == 2
    assert segs[0]["start"] == 1.0 and segs[0]["end"] == 4.0
    assert segs[0]["text"] == "你好 世界"  # 去除 <c> 标签
    assert segs[1]["text"] == "第二行字幕" and segs[1]["end"] == 8.0
    print("[parse_subtitle_file] vtt ok")


def test_parse_srt():
    srt = (
        "1\n00:00:01,000 --> 00:00:04,000\n第一句\n\n"
        "2\n00:00:05,000 --> 00:00:08,000\n第二句\n"
    )
    path = _write_tmp(srt, ".srt")
    try:
        segs = subtitles._parse_subtitle_file(path)
    finally:
        path.unlink(missing_ok=True)
    assert len(segs) == 2
    assert segs[0]["text"] == "第一句"
    assert segs[1]["start"] == 5.0 and segs[1]["text"] == "第二句"
    print("[parse_subtitle_file] srt ok")


def test_dedupe():
    segs = [
        {"start": 0, "end": 1, "text": "你好"},
        {"start": 1, "end": 2, "text": "你好"},  # 相邻重复 -> 去掉
        {"start": 2, "end": 3, "text": "世界"},
        {"start": 3, "end": 4, "text": ""},       # 空 -> 去掉
    ]
    out = subtitles._dedupe(segs)
    assert [s["text"] for s in out] == ["你好", "世界"]
    print("[dedupe] ok")


def test_apply_cookies():
    os.environ.pop("COOKIE_FILE", None)
    os.environ.pop("COOKIES_FROM_BROWSER", None)
    # 无配置 -> 不注入
    opts = subtitles._subtitle_opts("t")
    assert "cookiefile" not in opts and "cookiesfrombrowser" not in opts

    # COOKIES_FROM_BROWSER -> (browser, profile, keyring, container)
    os.environ["COOKIES_FROM_BROWSER"] = "chrome"
    assert subtitles._subtitle_opts("t")["cookiesfrombrowser"] == ("chrome", None, None, None)
    os.environ["COOKIES_FROM_BROWSER"] = "chrome:Profile 1"
    assert subtitles._subtitle_opts("t")["cookiesfrombrowser"] == ("chrome", "Profile 1", None, None)

    # COOKIE_FILE 指向不存在文件 -> 忽略
    os.environ["COOKIES_FROM_BROWSER"] = ""
    os.environ["COOKIE_FILE"] = "D:/no/such/cookies.txt"
    assert "cookiefile" not in subtitles._subtitle_opts("t")

    # COOKIE_FILE 真实存在 -> 注入且优先于 browser
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write("# Netscape HTTP Cookie File\n")
        cf = f.name
    try:
        os.environ["COOKIE_FILE"] = cf
        os.environ["COOKIES_FROM_BROWSER"] = "edge"
        opts = subtitles._subtitle_opts("t")
        assert opts.get("cookiefile") == cf and "cookiesfrombrowser" not in opts
    finally:
        os.unlink(cf)
        os.environ.pop("COOKIE_FILE", None)
        os.environ.pop("COOKIES_FROM_BROWSER", None)
    print("[apply_cookies] ok")


def test_asr_chain():
    # 无任何 Key -> 空链、不可用
    _clear_asr_env()
    assert asr.load_asr_chain() == []
    assert asr.asr_available() is False
    assert asr.asr_label() == ""

    # 仅硅基流动
    os.environ["SILICONFLOW_API_KEY"] = "sk-sf"
    chain = asr.load_asr_chain()
    assert [c.provider for c in chain] == ["siliconflow"]
    assert chain[0].style == "transcriptions"
    assert chain[0].model == "FunAudioLLM/SenseVoiceSmall"
    assert chain[0].base_url == "https://api.siliconflow.cn/v1"
    assert asr.asr_available() is True and asr.asr_label() == "硅基流动 SenseVoice"

    # 硅基流动 + DashScope -> 默认顺序 [siliconflow, dashscope]
    os.environ["DASHSCOPE_API_KEY"] = "sk-ds"
    assert [c.provider for c in asr.load_asr_chain()] == ["siliconflow", "dashscope"]
    ds = asr.load_asr_chain()[1]
    assert ds.style == "chat_audio" and ds.model == "qwen3-asr-flash"

    # ASR_PROVIDER 显式改序
    os.environ["ASR_PROVIDER"] = "dashscope,siliconflow"
    assert [c.provider for c in asr.load_asr_chain()] == ["dashscope", "siliconflow"]
    os.environ.pop("ASR_PROVIDER")

    # 仅 DashScope（QWEN_API_KEY 别名亦可）
    _clear_asr_env()
    os.environ["QWEN_API_KEY"] = "sk-qw"
    chain = asr.load_asr_chain()
    assert [c.provider for c in chain] == ["dashscope"] and chain[0].api_key == "sk-qw"

    # 自定义 OpenAI 兼容端点（最优先）
    _clear_asr_env()
    os.environ["ASR_API_KEY"] = "sk-custom"
    os.environ["ASR_BASE_URL"] = "https://api.groq.com/openai/v1"
    os.environ["ASR_MODEL"] = "whisper-large-v3-turbo"
    os.environ["SILICONFLOW_API_KEY"] = "sk-sf"
    chain = asr.load_asr_chain()
    assert [c.provider for c in chain] == ["custom", "siliconflow"]
    assert chain[0].model == "whisper-large-v3-turbo"
    _clear_asr_env()
    print("[asr] provider chain / order / custom ok")


def test_asr_synthesize_segments():
    text = "第一句话。第二句话！第三句话？还没结束呢"
    segs = asr._synthesize_segments(text)
    assert segs and all("text" in s for s in segs)
    assert "".join(s["text"] for s in segs) == text  # 不丢字
    assert asr._synthesize_segments("   ") == []
    print("[asr] synthesize segments ok")


def test_no_subtitle_hint():
    # 门面按平台给出差异化、可操作提示（离线，不触网）
    d = tc._no_subtitle_hint("https://v.douyin.com/abc/", Exception("x"))
    assert "抖音" in d and "SILICONFLOW_API_KEY" in d
    b = tc._no_subtitle_hint("https://www.bilibili.com/video/BV1xx", Exception("x"))
    assert "B站" in b and "COOKIES_FROM_BROWSER" in b
    o = tc._no_subtitle_hint("https://youtube.com/watch?v=x", Exception("未找到字幕"))
    assert "SILICONFLOW_API_KEY" in o
    print("[asr] no-subtitle hint ok")


if __name__ == "__main__":
    test_config_no_key()
    test_config_providers()
    test_config_aliases()
    test_extract_json()
    test_normalize()
    test_summarize_no_key()
    test_parse_ts()
    test_parse_vtt()
    test_parse_srt()
    test_dedupe()
    test_apply_cookies()
    test_asr_chain()
    test_asr_synthesize_segments()
    test_no_subtitle_hint()
    print("TRANSCRIBE & AI TEST PASSED")
