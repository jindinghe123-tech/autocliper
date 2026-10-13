"""Fast unit tests for pure logic (no ffmpeg / network needed)."""
import os
import tempfile

os.environ.setdefault("AUTOSTUDIO_HOME", tempfile.mkdtemp(prefix="autostudio_test_"))

import pytest  # noqa: E402

from autostudio.agent.loop import _contiguous_tools, prepare_history  # noqa: E402
from autostudio.media import subs  # noqa: E402
from autostudio.media.ffmpeg import atempo_chain, parse_size, parse_time  # noqa: E402
from autostudio.providers.anthropic import AnthropicProvider  # noqa: E402
from autostudio.providers.openai_chat import OpenAIChatProvider  # noqa: E402
from autostudio.providers.responses import ResponsesProvider  # noqa: E402
from autostudio.tools import REGISTRY  # noqa: E402
from autostudio.tools.video import complement  # noqa: E402


@pytest.mark.parametrize("value,expected", [
    (12, 12.0), ("12.5", 12.5), ("1:02.5", 62.5), ("01:02:03,250", 3723.25), ("90s", 90.0), ("2m", 120.0),
    ("500ms", 0.5), (None, None),
])
def test_parse_time(value, expected):
    assert parse_time(value) == expected


def test_parse_size_and_atempo():
    assert parse_size("9:16") == (1080, 1920)
    assert parse_size("1280x721") == (1280, 720)  # forced even
    assert atempo_chain(4).count("atempo") == 2
    assert atempo_chain(0.25).count("atempo") == 2


def test_complement_with_padding():
    keep = complement([(2, 4), (6, 7)], 10, pad=0.1)
    assert keep == [(0.0, 2.1), (3.9, 6.1), (6.9, 10)]


SRT = """1
00:00:00,500 --> 00:00:02,000
你好，世界

2
00:00:02,500 --> 00:00:04,000
<i>Second</i> line
"""


def test_srt_roundtrip(tmp_path):
    cues = subs.parse_srt_vtt(SRT)
    assert [c.text for c in cues] == ["你好，世界", "Second line"]
    p = tmp_path / "a.vtt"
    subs.save(p, cues)
    again = subs.load(p)
    assert again[1].start == pytest.approx(2.5)
    ass = tmp_path / "a.ass"
    subs.save(ass, cues, width=1080, height=1920, style={"box": True, "animation": "pop"})
    assert [c.text for c in subs.load(ass)] == ["你好，世界", "Second line"]


def test_segment_words_cjk_breaks_on_punctuation():
    words = [{"start": i * 0.3, "end": i * 0.3 + 0.25, "word": w} for i, w in enumerate(list("大家好，欢迎来到我的频道。今天聊聊剪辑"))]
    cues = subs.segment_words(words, max_chars=8, strip_punct=True)
    assert cues[0].text == "大家好"  # breaks at the full-width comma, which is then stripped
    assert all(subs.text_len(c.text) <= 8 for c in cues)
    assert all(c.end > c.start for c in cues)


def test_prepare_history_closes_dangling_tool_calls():
    hist = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "name": "x", "arguments": "{}"},
                                                             {"id": "b", "name": "y", "arguments": "{}"}]},
        {"role": "tool", "tool_call_id": "a", "name": "x", "content": "ok"},
        {"role": "user", "content": "next"},
    ]
    out = prepare_history(_contiguous_tools(hist), vision=False)
    ids = [m.get("tool_call_id") for m in out if m["role"] == "tool"]
    assert sorted(ids) == ["a", "b"]


def test_provider_conversions():
    hist = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "calling", "tool_calls": [{"id": "c1", "name": "media_info", "arguments": '{"path":"a.mp4"}'}]},
        {"role": "tool", "tool_call_id": "c1", "name": "media_info", "content": "{}"},
    ]
    chat = OpenAIChatProvider("http://x/v1", "k", "m").convert("sys", hist)
    assert chat[0]["role"] == "system" and chat[2]["tool_calls"][0]["function"]["name"] == "media_info"
    items = ResponsesProvider("m", api_key="k").convert(hist)
    assert [i["type"] for i in items] == ["message", "message", "function_call", "function_call_output"]
    msgs = AnthropicProvider("k", "m").convert(hist)
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[1]["content"][1] == {"type": "tool_use", "id": "c1", "name": "media_info", "input": {"path": "a.mp4"}}


def test_model_list_filter_newest_first():
    from autostudio.providers.catalog import sort_and_filter

    items = [{"id": "gpt-old", "created": 100}, {"id": "text-embedding-3-large", "created": 999},
             {"id": "gpt-6-sol", "created": 300}, {"id": "whisper-1", "created": 500}, {"id": "models/gemini-x", "created": 200}]
    assert sort_and_filter(items) == ["gpt-6-sol", "gemini-x", "gpt-old"]


def test_reasoning_effort_resolution(monkeypatch):
    from autostudio.providers import catalog

    meta = {"slug": "m", "default_effort": "low", "efforts": ["low", "medium", "high", "max"]}
    monkeypatch.setattr(catalog, "model_meta", lambda slug: meta)
    eff = lambda want: ResponsesProvider("m", chatgpt=True, reasoning_effort=want).reasoning_effort  # noqa: E731
    assert eff("auto") == "low"
    assert eff("high") == "high"
    assert eff("ultra") == "max"      # unsupported -> closest lower level
    assert eff("xhigh") == "high"
    assert eff("minimal") == "low"    # nothing lower -> lowest supported


def test_legacy_chatgpt_defaults_migrate(tmp_path):
    import json

    from autostudio.config import CHATGPT_MODELS, Settings

    f = tmp_path / "settings.json"
    f.write_text(json.dumps({"active": {"provider": "chatgpt", "model": "gpt-5.5"}, "providers": {"chatgpt": {
        "type": "chatgpt", "models": ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex", "gpt-5.2"]}}}), "utf-8")
    s = Settings(f).get()
    assert s["providers"]["chatgpt"]["models"] == CHATGPT_MODELS
    assert s["active"]["model"] == CHATGPT_MODELS[0]


def test_tts_split_and_chunk_timing():
    from autostudio.media import tts

    text = "第一句话。" * 80
    chunks = tts.split_text(text, 100)
    assert all(len(c) <= 100 for c in chunks) and "".join(chunks) == text
    cues = tts.cues_for("你好世界。再见。", {"chunks": [("你好世界。", 2.0), ("再见。", 1.0)]}, 3.0, 16)
    assert cues[0].text == "你好世界" and cues[-1].start == pytest.approx(2.0) and cues[-1].end == pytest.approx(3.0)


def test_tts_engine_order(monkeypatch):
    from autostudio.media import local_tts
    from autostudio.media.tts import engine_order

    monkeypatch.setattr(local_tts, "available", lambda: False)
    assert engine_order({}) == ["edge", "system"]
    assert engine_order({"volc": {"appid": "1"}, "clone": {"ref_audio": "a.wav"}}) == ["edge", "volcengine", "gptsovits", "system"]
    assert engine_order({"engine": "gptsovits"}) == ["gptsovits"]
    monkeypatch.setattr(local_tts, "available", lambda: True)
    assert engine_order({}) == ["edge", "local", "system"]   # offline neural voice before the OS voice


def test_local_tts_voice_and_sentences():
    from autostudio.media import local_tts
    from autostudio.media.tts import _sentences

    assert local_tts.pick_voice("温柔女声", None, True)["sid"] == 12
    assert local_tts.pick_voice(None, "解说男声", True)["sid"] == 72
    assert local_tts.pick_voice("kokoro:61", None, True)["sid"] == 61
    assert local_tts.pick_voice("zh-CN-YunxiNeural", None, False) == local_tts.DEFAULT_EN
    parts = _sentences("第一句。第二句很长" + "，再长一点" * 12 + "！")
    assert parts[0] == "第一句。" and all(len(p) <= 48 for p in parts) and "".join(parts).startswith("第一句。第二句很长")


def test_shot_split_and_motion_labels():
    from autostudio.tools.understand import _motion_label, _shots

    motion = [(t / 4, 0.9 if t in (16, 32) else 0.0, 3.0) for t in range(48)]
    assert _shots(motion, 12.0, 0.3, 0.8) == [[0.0, 4.0], [4.0, 8.0], [8.0, 12.0]]
    assert [_motion_label(v) for v in (0.2, 3, 8, 30)] == ["静止", "平缓", "中等", "剧烈"]


def test_tool_schemas_are_valid():
    assert len(REGISTRY) >= 45
    for t in REGISTRY.values():
        sch = t.schema()
        assert sch["parameters"]["type"] == "object"
        for req in sch["parameters"]["required"]:
            assert req in sch["parameters"]["properties"], (t.name, req)
        assert len(t.description) > 5


def test_images_are_capped_and_compressed(tmp_path):
    import subprocess

    from autostudio.agent.loop import MAX_IMAGES, prepare_history
    from autostudio.media.ffmpeg import ffmpeg_bin
    from autostudio.providers.base import compact_image, image_data_url, lean_images

    big = tmp_path / "big.png"
    subprocess.run([ffmpeg_bin(), "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=2400x1600", "-frames:v", "1", str(big)], check=True)
    small, mime = compact_image(big)
    assert mime == "image/jpeg" and small != big and small.stat().st_size < big.stat().st_size
    assert image_data_url(str(big)).startswith("data:image/jpeg;base64,")
    tiny = tmp_path / "tiny.jpg"
    subprocess.run([ffmpeg_bin(), "-v", "error", "-f", "lavfi", "-i", "color=red:s=64x64", "-frames:v", "1", str(tiny)], check=True)
    assert compact_image(tiny) == (tiny, "image/jpeg")   # small enough: sent as is

    msgs = [{"role": "tool", "tool_call_id": f"c{i}", "name": "t", "content": "", "images": [str(tiny)] * 3} for i in range(4)]
    kept = [m["images"] for m in prepare_history(msgs, True)]
    assert sum(len(x) for x in kept) == MAX_IMAGES and kept[-1] == [str(tiny)] * 3
    assert [len(m["images"]) for m in lean_images(msgs)] == [0, 0, 0, 3]


def test_transient_gateway_errors_are_retried_with_fewer_images(monkeypatch):
    import asyncio

    from autostudio.providers import responses
    from autostudio.providers.base import TurnResult

    calls = []

    async def once(self, system, messages, tools, emit, force_refresh):
        calls.append(sum(len(m.get("images") or []) for m in messages))
        if len(calls) == 1:
            raise responses._Transient(507, "too big")
        return TurnResult(text="ok")

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(responses.ResponsesProvider, "_stream_once", once)
    monkeypatch.setattr(responses.asyncio, "sleep", no_sleep)
    p = responses.ResponsesProvider.__new__(responses.ResponsesProvider)
    msgs = [{"role": "user", "content": "a", "images": ["x.jpg", "y.jpg"]}, {"role": "user", "content": "b", "images": ["z.jpg"]}]
    res = asyncio.run(p.stream("sys", msgs, [], None))
    assert res.text == "ok" and calls == [3, 1]
