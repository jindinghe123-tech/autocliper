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
                  {"id": "a2", "role": "voice", "at": 1.5, "duration": 4}])
    warn = "\n".join(tl.check(p))
    for expected in ("重叠", "阅读速度", "标题和按钮", "网址", "画面只有"):
        assert expected in warn, expected
    assert "背景音乐" not in warn   # facts only: having no music is a choice, not a problem


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
    with pytest.raises(ToolError, match="创作意图"):
        asyncio.run(ed.timeline_render(ctx, "demo"))
    with pytest.raises(ToolError, match="太少"):
        asyncio.run(ed.project_brief(ctx, "demo", "做一个好看的视频"))
    assert asyncio.run(ed.project_brief(ctx, "demo", "给宿舍党看的悬念科普。" * 12))["ok"]
    with pytest.raises(ToolError, match="没有「sparkle」这个特效"):
        asyncio.run(ed.timeline_edit(ctx, "demo", [{"op": "add_clip", "color": "#000", "duration": 2, "effects": ["sparkle"]}]))
    r = asyncio.run(ed.timeline_edit(ctx, "demo", [
        {"op": "add_clip", "color": "#000", "duration": 2},
        {"op": "add_clip", "color": "#fff", "duration": 3},
        {"op": "split", "id": "v2", "at": 1},
        {"op": "move", "id": "v1", "index": 3},
        {"op": "add_text", "text": "钩子", "at": 0, "duration": 1.5, "font": "楷体", "x": "(w-text_w)/2+100*sin(t*4)", "y": 300},
        {"op": "add_clip", "lavfi": "gradients=s=320x240:speed=0.02", "duration": 2, "vf": "hue=s=0"},
    ]))
    _d, p = tl.load(Path(tmp_path), "demo")
    assert [c["id"] for c in p["video"]] == ["v2", "v3", "v1", "v4"] and [c["duration"] for c in p["video"]] == [1.0, 2.0, 2, 2]
    assert p["video"][3]["kind"] == "gen" and p["texts"][0]["x"].startswith("(w-text_w)")
    assert r["rev"] == p["rev"] and "钩子" in r["timeline"]


def test_studio_run_executes_code_and_reports_files(tmp_path):
    from autostudio.tools.workbench import studio_run

    async def emit(_ev):
        return None

    ctx = ToolContext(session_id="s", workspace=tmp_path, emit=emit)
    code = (
        "from autostudio.studio import *\n"
        "img = canvas(64, 64, (255, 0, 0, 255))\n"
        "img.save('red.png')\n"
        "print('size', probe('red.png')['width'] or 64, ease(1, 'out_back'))\n"
    )
    r = asyncio.run(studio_run(ctx, code))
    assert r["ok"] and "size 64 1" in r["output"] and r["files"][0].endswith("red.png")
    assert (tmp_path / r["script"]).exists()                      # kept for reuse
    bad = asyncio.run(studio_run(ctx, "raise ValueError('boom')"))
    assert not bad["ok"] and "boom" in bad["output"]


def test_delivery_gate_needs_watching_and_review(tmp_path, monkeypatch):
    import subprocess

    from autostudio.media.ffmpeg import ffmpeg_bin
    from autostudio.tools import workbench

    monkeypatch.setattr(workbench, "LOG", tmp_path / "log.json")
    vid = tmp_path / "cut.mp4"
    subprocess.run([ffmpeg_bin(), "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x240:d=4", "-f", "lavfi", "-i",
                    "sine=f=440:d=4", "-shortest", "-pix_fmt", "yuv420p", str(vid)], check=True)

    async def emit(_ev):
        return None

    ctx = ToolContext(session_id="s", workspace=tmp_path, emit=emit)
    pub, cover = {"title": "t", "description": "d"}, {"title": "封面"}
    r = asyncio.run(ed.deliver(ctx, "cut.mp4", pub, cover))
    assert not r["ok"] and any("开头" in f for f in r["fix_these"]) and any("review_cut" in f for f in r["fix_these"])
    for a, b in ((0, 2), (1.5, 2.5), (3, 4)):
        workbench.note_watch(vid, a, b)
    workbench._update(vid, lambda rec: rec["reviews"].append({"verdict": "revise", "must_fix": [{"issue": "x"}]}))
    r = asyncio.run(ed.deliver(ctx, "cut.mp4", pub, cover))
    assert not r["ok"] and any("必须修改" in f for f in r["fix_these"])
    def critic_says(accepted):
        async def judge(_ctx, _src, must, _responses):
            return [{"item": i, "accepted": accepted, "why": "理由"} for i in range(1, len(must) + 1)]
        return judge

    monkeypatch.setattr(workbench, "judge_responses", critic_says(False))
    r = asyncio.run(ed.deliver(ctx, "cut.mp4", pub, cover, responses=["这是风格"]))
    assert not r["ok"] and any("不接受" in f for f in r["fix_these"])
    monkeypatch.setattr(workbench, "judge_responses", critic_says(True))
    r = asyncio.run(ed.deliver(ctx, "cut.mp4", pub, cover, responses=["这是测试图，不需要改"]))
    assert r["ok"] and r["delivered"], r
    assert "审片" not in (tmp_path / "outputs" / "cut_交付" / "发布文案.md").read_text("utf-8")
    assert (tmp_path / "outputs" / "cut_交付" / "审片记录.md").exists()
    workbench.note_watch(vid, 0, 1)
    vid.write_bytes(vid.read_bytes() + b"\0")                    # a new version resets what was watched and reviewed
    assert workbench.cut_record(vid) == {"watched": [], "reviews": []}
