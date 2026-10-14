"""The agent's workbench: run its own Python against pixels and samples, and get an honest review of a cut.

studio_run   the agent writes code (autostudio.studio gives frames, audio, fonts, voices, ffmpeg) — no ceiling
review_cut   an independent, demanding editor watches the whole cut and lists what must change
watch log    every file the agent looks at with video_watch is recorded, so delivery can prove it was seen
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import autostudio

from .. import state
from ..config import DATA_DIR
from ..media import ffmpeg as ff
from ..media import subs as sublib
from .registry import ToolContext, ToolError, integer, obj, s, tool

REPO_ROOT = Path(autostudio.__file__).resolve().parent.parent
LOG = DATA_DIR / "cut_log.json"     # path -> {stamp, watched: [[a, b]], reviews: [...]}
MEDIA = ff.VIDEO_EXTS | ff.IMAGE_EXTS | ff.AUDIO_EXTS | {".webm", ".mov", ".gif"}
SKIP_DIRS = {"scripts", ".tmp", ".frames", ".trash", "__pycache__"}


# ---------------------------------------------------------------------------
# what has been watched / reviewed

def _stamp(p: Path) -> str:
    st = p.stat()
    return f"{st.st_size}:{int(st.st_mtime)}"


def _read_log() -> dict[str, Any]:
    try:
        return json.loads(LOG.read_text("utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def cut_record(path: Path) -> dict[str, Any]:
    """Watches and reviews of exactly this version of the file (a re-render resets them)."""
    key = str(path.resolve())
    rec = _read_log().get(key) or {}
    if not path.exists() or rec.get("stamp") != _stamp(path):
        return {"watched": [], "reviews": []}
    return rec


def _update(path: Path, fn: Any) -> None:
    key = str(path.resolve())
    log = _read_log()
    rec = log.get(key) or {}
    if rec.get("stamp") != _stamp(path):
        rec = {"stamp": _stamp(path), "watched": [], "reviews": []}
    fn(rec)
    log[key] = rec
    if len(log) > 500:
        for k in list(log)[:100]:
            log.pop(k, None)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text(json.dumps(log, ensure_ascii=False), "utf-8")


def note_watch(path: Path, a: float, b: float) -> None:
    try:
        _update(path, lambda r: r["watched"].append([round(a, 2), round(b, 2)]))
    except Exception:  # noqa: BLE001 - bookkeeping must never break watching
        pass


# ---------------------------------------------------------------------------
# studio_run

def _snapshot(root: Path) -> dict[str, float]:
    out: dict[str, float] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            p = Path(dirpath) / f
            try:
                out[str(p)] = p.stat().st_mtime
            except OSError:
                pass
        if len(out) > 20000:
            break
    return out


@tool("studio_run",
      "写一段 Python 并运行，直接在像素和音频样本层面工作。现成工具做不到、做不好的，都自己写：逐帧绘制动态图形、文字动画、"
      "图解和数据动画、遮罩与合成、自定义转场、粒子和生成式背景、调色曲线、音频处理与合成……\n"
      "`from autostudio.studio import *` 提供：VideoWriter(path, w, h, fps, alpha=False, audio=None)（逐帧写视频；"
      "alpha=True 写透明叠加层 .webm/.mov，可放进时间线叠加）、read_frames / frame_at（读帧为 numpy）、canvas / draw / font"
      "（PIL 绘图，font 可用任何已安装或已下载的字体）、read_audio / write_audio（numpy 音频）、tts(text, voice) → 音频和逐句时间、"
      "beats(音乐) → 节拍、ffmpeg(*args) / probe(path)、lerp / clamp / ease(t, 'out_back' 等缓动)，以及 numpy (np)、PIL、math。\n"
      "工作目录是项目文件夹（不传 project 时为工作区）；print 的内容会返回给你；新生成的图片和视频会自动预览。"
      "脚本保存在 scripts/ 下，可以改了再跑。",
      obj(["code"], code=s("完整的 Python 代码"), project=s("在哪个剪辑项目的文件夹里运行（默认工作区）"),
          timeout=integer("超时秒数，默认 900")),
      category="creative", dangerous=True)
async def studio_run(ctx: ToolContext, code: str, project: str | None = None, timeout: int = 900) -> dict[str, Any]:
    from .. import timeline as tl

    cwd = tl.project_dir(ctx.workspace, project) if project else ctx.workspace
    if project and not (cwd / "timeline.json").exists():
        raise ToolError(f"项目「{project}」不存在")
    scripts = cwd / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    script = scripts / f"run_{time.strftime('%Y%m%d-%H%M%S')}.py"
    script.write_text(code, "utf-8")
    before = _snapshot(cwd)
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
           "PYTHONIOENCODING": "utf-8", "AUTOSTUDIO_WORKSPACE": str(ctx.workspace)}
    last = {"t": 0.0}

    def on_line(line: str) -> None:
        if time.monotonic() - last["t"] > 1.5 and line.strip():
            last["t"] = time.monotonic()
            import asyncio

            asyncio.get_running_loop().create_task(ctx.progress(0.5, line.strip()[:80]))

    t0 = time.time()
    try:
        code_, out, err = await ff.run_proc([sys.executable, "-X", "utf8", str(script)], cwd=cwd,
                                            timeout=max(10, min(3600, int(timeout or 900))), on_stdout=on_line, env=env)
    except TimeoutError as e:
        raise ToolError(f"运行超过 {timeout} 秒被终止。把任务拆小，或先用低分辨率/少量帧试。") from e
    after = _snapshot(cwd)
    changed = [p for p, m in after.items() if before.get(p) != m]
    media = [p for p in changed if Path(p).suffix.lower() in MEDIA]
    text = (out + ("\n" + err if err.strip() else "")).strip()
    if len(text) > 6000:
        text = text[:1500] + "\n…（中间省略）…\n" + text[-4000:]
    res: dict[str, Any] = {"ok": code_ == 0, "exit_code": code_, "seconds": round(time.time() - t0, 1),
                           "script": ctx.rel(script), "output": text or "（没有输出）",
                           "created_or_changed": [ctx.rel(p) for p in changed[:40]], "files": media[:6]}
    if code_ != 0:
        res["error"] = "脚本出错了，看 output 里的报错，改了再跑。"
    return res


# ---------------------------------------------------------------------------
# review_cut

REVIEW_PROMPT = """你是一位眼光很高、说话直接的短视频总编。作者把一条成片交给你审。你会看到整条片子按时间顺序的抽帧、口播文字稿、
音量曲线、画面变化曲线，以及作者自己写的创作意图。抽帧之间的运动你看不到，画面动没动以“画面变化”曲线为准。

请站在真实观众和平台的角度审片，只说你从材料里真正看到的问题，每条都要带时间点、说清楚为什么是问题、怎么改更好：
- 开头 3 秒能不能让人停下来；节奏有没有拖沓或者乱；画面和口播是否对得上；有没有看不清、读不完、被平台按钮挡住的字；
- 画面质量（模糊、拉伸、黑边、重复使用同一画面、长时间静止）、声音（太小、太吵、人声被盖住、突兀的静音）；
- 有没有不该出现在画面里的东西（网址、来源说明、免责声明、对制作过程的描述、题外话）；
- 它是不是看起来像模板、像流水线产品：版式/字体/节奏/转场是否千篇一律、和题材气质是否匹配、有没有作者自己的想法；
- 是否兑现了作者写的创作意图。

只输出 JSON：
{"verdict": "ship" 或 "revise", "score": 1-10,
 "must_fix": [{"time": "mm:ss 或 区间", "issue": "问题", "fix": "具体怎么改"}],
 "should": [{"time": "...", "issue": "...", "fix": "..."}],
 "template_feel": "像不像模板，具体哪里",
 "best_moment": "最好的一处"}
标准要高：
- "ship" 的意思是你愿意不改一帧就把它推上首页，score ≥ 8。达不到就是 revise，并在 must_fix 里写清楚差在哪。
- 观众一眼就会觉得是模板、PPT、流水线产品的地方，是 must_fix，不是建议。
- 画面连续好几秒没有新的视觉信息、只有字幕在换（像一页页翻幻灯片、像在朗读通知），是 must_fix。
- 事实可能误导人、字看不清或读不完、画面和口播对不上，是 must_fix。
有任何 must_fix 时 verdict 必须是 revise。真的没有问题就别硬凑。"""


async def _review_material(ctx: ToolContext, src: Path, info: dict[str, Any]) -> tuple[list[Path], str, str]:
    from .understand import contact_sheets, loudness_profile, motion_profile

    dur = float(info.get("duration") or 0)
    n = int(min(36, max(12, dur * 0.9)))
    items = [(dur * (i + 0.5) / n, f"{int(dur * (i + 0.5) / n // 60):02d}:{dur * (i + 0.5) / n % 60:04.1f}") for i in range(n)]
    portrait = (info.get("height") or 0) > (info.get("width") or 0)
    sheets = await contact_sheets(ctx, src, items, portrait, cols=6 if portrait else 4, per_sheet=18 if portrait else 12,
                                  name=f"{src.stem}_review")
    # transcript: captions next to the file, else those of the project the render belongs to, else speech recognition
    cues: list[sublib.Cue] = []
    side = src.with_suffix(".srt")
    if side.exists():
        cues = sublib.load(side)
    elif src.parent.name == "renders" and (src.parent.parent / "timeline.json").exists():
        from .. import timeline as tl

        cues = tl.subtitle_cues(json.loads((src.parent.parent / "timeline.json").read_text("utf-8")))
    if not cues and info.get("has_audio"):
        try:
            from ..media import asr

            wav = ctx.tmpdir() / "a.wav"
            await ff.ffmpeg(["-i", str(src), "-vn", "-ac", "1", "-ar", "16000", str(wav)])
            import asyncio

            res = await asyncio.to_thread(asr.transcribe_local, str(wav), model="small")
            cues = [sublib.Cue(float(sg["start"]), float(sg["end"]), sg["text"]) for sg in res.get("segments") or []]
        except Exception:  # noqa: BLE001 - review still works from pictures alone
            cues = []
    transcript = "\n".join(f"{c.start:5.1f}-{c.end:5.1f} {c.text}" for c in cues) or "（没有口播或无法识别）"
    vol = await loudness_profile(src, step=1.0)
    bars = "▁▂▃▄▅▆▇█"
    curve = "".join(bars[min(7, max(0, int((v + 50) / 50 * 8)))] if v is not None else " " for v in vol)
    # stills can't show movement between them, so say how much the picture changes each second
    per_sec: dict[int, float] = {}
    for t, _cut, mv in await motion_profile(src, dur, fps=4.0):
        per_sec[int(t)] = max(per_sec.get(int(t), 0.0), mv)
    steps = (0.05, 0.3, 0.8, 1.5, 3, 6, 12)
    moving = "".join("·" if (m := per_sec.get(i, 0.0)) < steps[0] else bars[sum(m >= x for x in steps[1:])]
                     for i in range(int(dur) + 1))
    still = moving.count("·")
    return sheets, transcript, (f"音量，每格 1 秒（-50~0 dB）：{curve}\n"
                                f"画面变化，每格 1 秒（· = 这一秒画面完全没动，▁ 只有少量像素在变，█ 大幅运动或切换）：{moving}\n"
                                f"完全静止的秒数：{still} / {len(moving)}")


RULING_PROMPT = """你是刚才审这条片子的总编。作者没有按你的必须修改项去改，而是给出了理由。逐条判断理由站不站得住：
- 站得住：作者拿出具体证据说明你看错了；或者这是有明确目的的创作取舍，照你的建议改反而会损害这条片子想达到的效果。
- 站不住：用“这是风格”“简洁”“观众能看懂”“不必追求节奏”之类的话，为模板感、PPT 感、画面长时间静止、平庸开脱；
  事实可能误导人、字看不清或读不完、画面和口播对不上这类硬伤，除非作者证明你看错了，否则都站不住。
只输出 JSON：{"rulings": [{"item": 序号, "accepted": true 或 false, "why": "一句话说明"}]}，每条必须修改项都要有一条。"""


def _critic(ctx: ToolContext) -> Any:
    """The editor-in-chief: the same model the author uses, in a conversation of its own."""
    from ..providers import make_provider

    settings = state.settings.get()
    sess = state.db.get_session(ctx.session_id) or {}
    active = settings.get("active") or {}
    pid = sess.get("provider") or active.get("provider")
    model = sess.get("model") or (active.get("model") if pid == active.get("provider") else None)
    return make_provider(settings, pid, model, session_id=f"{ctx.session_id}-review")


async def _ask_critic(ctx: ToolContext, provider: Any, prompt: str, material: str,
                      images: list[Path] | None = None) -> tuple[dict[str, Any], str]:
    """One question to the critic; returns (the JSON object in its answer or {}, the raw answer)."""
    from ..providers import stream_retrying

    out: list[str] = []

    async def emit(ev: dict[str, Any]) -> None:
        if ev.get("type") == "text_delta":
            out.append(ev["delta"])

    async def on_retry(n: int, _e: Exception) -> None:
        out.clear()
        await ctx.progress(0.4, f"连接中断，重新问总编（第 {n} 次重试）")

    msg = {"role": "user", "content": material, "images": [str(p) for p in images or []] if provider.vision else []}
    res = await stream_retrying(provider, prompt, [msg], [], emit, on_retry)
    text = res.text or "".join(out)
    m = re.search(r"\{[\s\S]*\}", text)
    try:
        return (json.loads(m.group(0)) if m else {}), text
    except json.JSONDecodeError:
        return {}, text


async def judge_responses(ctx: ToolContext, src: Path, must: list[dict[str, Any]],
                          responses: list[str]) -> list[dict[str, Any]]:
    """The critic rules on the author's reasons for not fixing its must-fix items: [{item, accepted, why}]."""
    key = hashlib.sha1(json.dumps([must, responses], ensure_ascii=False).encode()).hexdigest()
    for r in cut_record(src).get("rulings") or []:
        if r.get("key") == key:
            return r["rulings"]
    material = ("你提出的必须修改项：\n"
                + "\n".join(f"{i}. [{m.get('time', '')}] {m.get('issue', '')}\n   你的建议：{m.get('fix', '')}"
                            for i, m in enumerate(must, 1))
                + "\n\n作者的回应：\n" + "\n".join(f"- {r}" for r in responses))
    await ctx.progress(0.5, "总编在看你的理由")
    data, _text = await _ask_critic(ctx, _critic(ctx), RULING_PROMPT, material)
    rulings = [r for r in data.get("rulings") or [] if isinstance(r, dict)]
    if rulings:
        _update(src, lambda rec: rec.setdefault("rulings", []).append({"key": key, "rulings": rulings}))
    return rulings


@tool("review_cut",
      "请一位严格的总编审片：它会看整条片子的抽帧、口播文字稿、音量和画面变化曲线，对照你的创作意图，列出必须修改的问题（带时间点和改法）、"
      "建议、是否像模板，以及最好的一处。交付前必须审过最后那一版；必须修改的问题要改掉，或在交付时给出站得住的理由。",
      obj(["video"], video=s("要审的成片文件"), intent=s("你的创作意图（不填则读取项目里写的）")),
      category="creative")
async def review_cut(ctx: ToolContext, video: str, intent: str | None = None) -> dict[str, Any]:
    src = ctx.resolve(video, must_exist=True)
    info = await ff.probe(src)
    if not info.get("has_video"):
        raise ToolError("这不是视频文件")
    await ctx.progress(0.1, "抽帧、整理文字稿")
    sheets, transcript, curve = await _review_material(ctx, src, info)
    if not intent and src.parent.name == "renders" and (src.parent.parent / "timeline.json").exists():
        intent = (json.loads((src.parent.parent / "timeline.json").read_text("utf-8")).get("brief") or {}).get("text")
    provider = _critic(ctx)
    material = (f"今天是 {time.strftime('%Y-%m-%d')}（你的知识可能比今天旧，不要因为日期或年份比你知道的新就判它错）。\n"
                f"成片：{src.name}，时长 {info.get('duration', 0):.1f} 秒，{info.get('width')}x{info.get('height')}\n\n"
                f"作者的创作意图：\n{intent or '（作者没有写）'}\n\n口播文字稿：\n{transcript}\n\n{curve}\n\n"
                f"附图是按时间顺序的抽帧（每格左上角是时间）。")
    if not provider.vision:
        material += "\n（当前模型看不了图片，只能根据文字稿和音量审片，画面问题请作者自己用 video_watch 检查。）"
    await ctx.progress(0.4, "总编审片中")
    review, text = await _ask_critic(ctx, provider, REVIEW_PROMPT, material, sheets)
    if not review:
        review = {"verdict": "revise", "must_fix": [], "raw": text[:3000]}
    if review.get("must_fix"):
        review["verdict"] = "revise"
    review["at"] = time.time()
    _update(src, lambda r: r["reviews"].append(review))
    return {"video": ctx.rel(src), **review, "images": [str(p) for p in sheets[:2]]}
