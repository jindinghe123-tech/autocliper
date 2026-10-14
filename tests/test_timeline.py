"""Timeline projects: timing with transitions, editor checks, subtitles from voice clips, and the guard rails."""
import asyncio
import os
import tempfile
from pathlib import Path

os.environ.setdefault("AUTOSTUDIO_HOME", tempfile.mkdtemp(prefix="autostudio_test_"))

import pytest  # noqa: E402

from autostudio import timeline as tl  # noqa: E402
from autostudio.media import subs as sublib  # noqa: E402
from autostudio.tools import editing as ed  # noqa: E402
from autostudio.tools.registry import ToolContext, ToolError  # noqa: E402


def _p(**kw):
    p = tl.new_project("t", 1080, 1920, 30)
    p.update(kw)
    return p


def test_main_track_times_account_for_transition_overlap():
    p = _p(video=[{"id": "v1", "kind": "color", "color": "#000", "duration": 3, "transition": {"type": "fade", "duration": 0.5}},
                  {"id": "v2", "kind": "video", "src": "x.mp4", "in": 1, "out": 5, "speed": 2, "transition": "cut"},
                  {"id": "v3", "kind": "image", "src": "a.jpg", "duration": 2}])
    times, pic = tl.main_track_times(p)
    assert times == [(0.0, 3.0), (2.5, 4.5), (4.5, 6.5)] and pic == 6.5


def test_checks_catch_editing_problems():
    p = _p(video=[{"id": "v1", "kind": "color", "color": "#000", "duration": 4}],
           texts=[{"id": "t1", "text": "这是一句非常非常非常长而且只闪一下的文字", "at": 1, "duration": 0.8, "position": "bottom"},
                  {"id": "t2", "text": "来源：www.example.com", "at": 2, "duration": 2}],
           audio=[{"id": "a1", "role": "voice", "at": 0, "duration": 2},
                  {"id": "a2", "role": "voice", "at": 1.5, "duration": 4}],
           brief={"duration": "30"})
    warn = "\n".join(tl.check(p))
    for expected in ("重叠", "读不完", "超过 14 个字", "平台的标题和按钮挡住", "网址", "画面只有", "目标 30s", "没有背景音乐"):
        assert expected in warn, expected


def test_subtitles_follow_voice_clips(tmp_path):
    voice = tmp_path / "line.mp3"
    voice.write_bytes(b"x")
    sublib.save(voice.with_suffix(".srt"), [sublib.Cue(0, 1, "第一句"), sublib.Cue(1, 2.5, "第二句")])
    p = _p(audio=[{"id": "a1", "role": "voice", "src": str(voice), "at": 10, "in": 0.5, "src_duration": 2.5}])
    cues = tl.subtitle_cues(p)
    assert [(round(c.start, 2), round(c.end, 2), c.text) for c in cues] == [(10.0, 10.5, "第一句"), (10.5, 12.0, "第二句")]


def test_forbidden_on_screen_text():
    for bad in ("来源：新华社", "据央视报道", "https://a.cn", "本视频由AI生成", "详见 weibo.com"):
        assert tl.FORBIDDEN_ON_SCREEN.search(bad), bad
    for ok in ("泡面不用煮熟", "差在温度", "你是哪一派？"):
        assert not tl.FORBIDDEN_ON_SCREEN.search(ok), ok


def test_brief_is_required_and_must_be_specific(tmp_path):
    async def emit(_ev):
        return None

    ctx = ToolContext(session_id="s", workspace=tmp_path, emit=emit)
    asyncio.run(ed.project_open(ctx, "demo"))
    with pytest.raises(ToolError, match="创意方案"):
        asyncio.run(ed.timeline_render(ctx, "demo"))
    with pytest.raises(ToolError, match="太笼统|缺少"):
        asyncio.run(ed.project_brief(ctx, "demo", {k: "好" for k in tl.BRIEF_FIELDS}))
    with pytest.raises(ToolError, match="没有「sparkle」这个特效"):
        asyncio.run(ed.timeline_edit(ctx, "demo", [{"op": "add_clip", "color": "#000", "duration": 2, "effects": ["sparkle"]}]))
    r = asyncio.run(ed.timeline_edit(ctx, "demo", [
        {"op": "add_clip", "color": "#000", "duration": 2},
        {"op": "add_clip", "color": "#fff", "duration": 3},
        {"op": "split", "id": "v2", "at": 1},
        {"op": "move", "id": "v1", "index": 3},
        {"op": "add_text", "text": "钩子", "at": 0, "duration": 1.5, "font": "楷体"},
    ]))
    _d, p = tl.load(Path(tmp_path), "demo")
    assert [c["id"] for c in p["video"]] == ["v2", "v3", "v1"] and [c["duration"] for c in p["video"]] == [1.0, 2.0, 2]
    assert r["rev"] == p["rev"] and "钩子" in r["timeline"]
