"""FastAPI app: REST + SSE for the web UI."""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import platform
import re
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import state
from .agent.loop import run_agent
from .agent.runs import RunManager
from .config import CHATGPT_MODELS, MEMORY_FILE, PROVIDER_PRESETS, VERSION  # noqa: F401
from .media import asr
from .media import ffmpeg as ff
from .media import subs as sublib
from .providers import ProviderError, make_provider
from .providers.catalog import fetch_chatgpt_catalog
from .providers.chatgpt_auth import chatgpt_auth
from .scheduler import TaskScheduler
from .tools import CATEGORY_LABELS, REGISTRY
from .media.tts import EDGE_PERSONAS, TTSError, synthesize
from .tools.creative import EFFECT_NAMES, TRANSITIONS, TTS_VOICES

log = logging.getLogger("autostudio")
WEB_DIR = Path(__file__).parent / "web"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
ALLOW_ANY_HOST = os.environ.get("AUTOSTUDIO_ALLOW_ANY_HOST") == "1"
_caps: dict[str, Any] | None = None


def _apply_runtime_settings() -> None:
    s = state.settings.get()
    ff.set_ffmpeg_override(s.get("ffmpeg_path") or "")
    hf = (s.get("asr") or {}).get("hf_endpoint")
    if hf:
        os.environ["HF_ENDPOINT"] = hf


async def sync_chatgpt_models(force: bool = True) -> list[dict[str, Any]]:
    """Pull the live ChatGPT model catalog into settings; move a stale default to the newest model."""
    catalog = await fetch_chatgpt_catalog(force=force)
    if not catalog:
        return catalog
    slugs = [m["slug"] for m in catalog]
    patch: dict[str, Any] = {"providers": {"chatgpt": {"models": slugs, "catalog": catalog}}}
    active = state.settings.get().get("active") or {}
    if active.get("provider") == "chatgpt" and active.get("model") not in slugs:
        patch["active"] = {"provider": "chatgpt", "model": slugs[0]}
    state.settings.update(patch)
    return catalog


async def _background_model_sync() -> None:
    if not chatgpt_auth.status().get("logged_in"):
        return
    try:
        await sync_chatgpt_models()
    except Exception as e:  # noqa: BLE001 — offline start must not break the app
        log.warning("ChatGPT model catalog refresh failed: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _apply_runtime_settings()
    state.runs = RunManager()
    state.scheduler = TaskScheduler()
    state.scheduler.start()
    sync_task = asyncio.create_task(_background_model_sync())
    yield
    sync_task.cancel()
    state.scheduler.shutdown()


app = FastAPI(title="AutoStudio", version=VERSION, lifespan=lifespan)


@app.middleware("http")
async def local_guard(request: Request, call_next):
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
    if not ALLOW_ANY_HOST and host not in LOCAL_HOSTS:
        return JSONResponse({"detail": "forbidden host"}, status_code=403)
    if request.url.path.startswith("/api/"):
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "cross-site request blocked"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-autostudio") != "1":
            return JSONResponse({"detail": "missing X-AutoStudio header"}, status_code=403)
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"  # always revalidate, so an upgrade shows up on refresh
    return response


@app.exception_handler(ProviderError)
async def provider_error(_req: Request, exc: ProviderError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


# ---------------------------------------------------------------------------
# static

@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse((WEB_DIR / "index.html").read_text("utf-8"), headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


# ---------------------------------------------------------------------------
# meta & settings

def _optional(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:
        return False


@app.get("/api/meta")
async def meta() -> dict[str, Any]:
    global _caps
    if _caps is None:
        _caps = await ff.capabilities()
    return {
        "version": VERSION,
        "os": f"{platform.system()} {platform.release()}",
        "workspace": str(state.settings.workspace),
        "ffmpeg": _caps,
        "features": {"faster_whisper": asr.faster_whisper_available(), "yt_dlp": _optional("yt_dlp"),
                     "edge_tts": _optional("edge_tts"), "numpy": _optional("numpy")},
        "presets": PROVIDER_PRESETS,
        "chatgpt_models": CHATGPT_MODELS,
        "tools": [{"name": t.name, "description": t.description, "category": t.category,
                   "category_label": CATEGORY_LABELS.get(t.category, t.category), "dangerous": t.dangerous}
                  for t in REGISTRY.values()],
        "transitions": TRANSITIONS,
        "effects": EFFECT_NAMES,
        "voices": TTS_VOICES,
        "personas": EDGE_PERSONAS,
    }


@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    return state.settings.public()


@app.put("/api/settings")
async def put_settings(patch: dict[str, Any] = Body(...)) -> dict[str, Any]:
    for pid, p in (patch.get("providers") or {}).items():
        if p is not None and not re.match(r"^[\w-]{1,40}$", pid):
            raise HTTPException(400, f"非法的提供方 id：{pid}")
    if "workspace" in patch and patch["workspace"]:
        Path(patch["workspace"]).expanduser().mkdir(parents=True, exist_ok=True)
    state.settings.update(patch)
    _apply_runtime_settings()
    global _caps
    if "ffmpeg_path" in patch:
        _caps = None
    return state.settings.public()


@app.post("/api/providers/{pid}/models")
async def provider_models(pid: str) -> dict[str, Any]:
    s = state.settings.get()
    cfg = (s.get("providers") or {}).get(pid)
    if not cfg:
        raise HTTPException(404, "提供方不存在")
    if cfg.get("type") == "chatgpt":
        try:
            catalog = await sync_chatgpt_models()
        except PermissionError as e:
            raise HTTPException(400, str(e)) from e
        except RuntimeError as e:
            raise HTTPException(502, str(e)) from e
        return {"models": [m["slug"] for m in catalog], "catalog": catalog}
    p = make_provider(s, pid, (cfg.get("models") or ["x"])[0] or "x")
    return {"models": await p.list_models()}


@app.post("/api/providers/{pid}/test")
async def provider_test(pid: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    s = state.settings.get()
    p = make_provider(s, pid, body.get("model") or None)
    out: list[str] = []

    async def emit(ev: dict[str, Any]) -> None:
        if ev.get("type") == "text_delta":
            out.append(ev["delta"])

    t0 = time.time()
    res = await p.stream("You are a helpful assistant.", [{"role": "user", "content": "只回复两个字：你好"}], [], emit)
    return {"ok": True, "reply": res.text.strip()[:200], "latency": round(time.time() - t0, 2),
            "model": getattr(p, "model", "")}


# ---------------------------------------------------------------------------
# ChatGPT sign-in

@app.get("/api/auth/chatgpt")
async def chatgpt_status() -> dict[str, Any]:
    return chatgpt_auth.status()


@app.post("/api/auth/chatgpt/login")
async def chatgpt_login() -> dict[str, Any]:
    url = await asyncio.to_thread(chatgpt_auth.start_login)
    return {"auth_url": url, "error": chatgpt_auth.last_error}


@app.post("/api/auth/chatgpt/callback")
async def chatgpt_callback(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    ok, msg = await asyncio.to_thread(chatgpt_auth.complete_from_url, str(body.get("url", "")))
    if not ok:
        raise HTTPException(400, msg)
    return chatgpt_auth.status()


@app.post("/api/auth/chatgpt/import")
async def chatgpt_import() -> dict[str, Any]:
    try:
        status = await asyncio.to_thread(chatgpt_auth.import_codex)
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    await _background_model_sync()
    return status


@app.post("/api/auth/chatgpt/logout")
async def chatgpt_logout() -> dict[str, Any]:
    chatgpt_auth.logout()
    return chatgpt_auth.status()


# ---------------------------------------------------------------------------
# sessions & runs

def _session_view(s: dict[str, Any]) -> dict[str, Any]:
    return {**s, "running": bool(state.runs and state.runs.is_running(s["id"]))}


@app.get("/api/sessions")
async def list_sessions() -> list[dict[str, Any]]:
    return [_session_view(s) for s in state.db.list_sessions()]


@app.post("/api/sessions")
async def create_session(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    s = state.settings.get()
    active = s.get("active") or {}
    sess = state.db.create_session(provider=body.get("provider") or active.get("provider"),
                                   model=body.get("model") or active.get("model"))
    return _session_view(sess)


@app.get("/api/sessions/{sid}")
async def get_session(sid: str) -> dict[str, Any]:
    sess = state.db.get_session(sid)
    if not sess:
        raise HTTPException(404, "对话不存在")
    msgs = state.db.get_messages(sid)
    for m in msgs:
        m.pop("reasoning_content", None)
    run = state.runs.get(sid) if state.runs else None
    return {**_session_view(sess), "messages": msgs,
            "last_seq": (run.events[-1]["seq"] if run and run.events else -1) if run and not run.done else None}


@app.patch("/api/sessions/{sid}")
async def patch_session(sid: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    if not state.db.get_session(sid):
        raise HTTPException(404, "对话不存在")
    state.db.update_session(sid, **body)
    if body.get("provider") and body.get("model"):
        state.settings.update({"active": {"provider": body["provider"], "model": body["model"]}})
    return _session_view(state.db.get_session(sid) or {})


@app.delete("/api/sessions/{sid}")
async def delete_session(sid: str) -> dict[str, Any]:
    if state.runs:
        state.runs.stop(sid)
    state.db.delete_session(sid)
    return {"ok": True}


@app.post("/api/sessions/{sid}/messages")
async def post_message(sid: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    if not state.db.get_session(sid):
        raise HTTPException(404, "对话不存在")
    assert state.runs
    if state.runs.is_running(sid):
        raise HTTPException(409, "正在运行中，请等待或先停止")
    text = str(body.get("content") or "").strip()
    attachments = [str(a) for a in body.get("attachments") or []]
    if not text and not attachments:
        raise HTTPException(400, "消息为空")
    # validate provider early so the user gets a clear error
    s = state.settings.get()
    sess = state.db.get_session(sid) or {}
    make_provider(s, sess.get("provider"), sess.get("model"))
    state.runs.start(sid, lambda run: run_agent(run, sid, text, attachments))
    return {"ok": True}


@app.get("/api/sessions/{sid}/events")
async def session_events(sid: str, request: Request, after: int = -1) -> StreamingResponse:
    assert state.runs

    async def gen():
        async for ev in state.runs.subscribe(sid, after):
            if await request.is_disconnected():
                break
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/sessions/{sid}/stop")
async def stop_session(sid: str) -> dict[str, Any]:
    return {"ok": bool(state.runs and state.runs.stop(sid))}


@app.post("/api/sessions/{sid}/approve")
async def approve(sid: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    ok = bool(state.runs and state.runs.resolve_approval(sid, str(body.get("id")), bool(body.get("approved"))))
    return {"ok": ok}


# ---------------------------------------------------------------------------
# tasks

def _task_view(t: dict[str, Any]) -> dict[str, Any]:
    return {**t, "enabled": bool(t["enabled"]), "allow_dangerous": bool(t["allow_dangerous"]),
            "next_run": state.scheduler.next_run(t["id"]) if state.scheduler else None}


@app.get("/api/tasks")
async def list_tasks() -> list[dict[str, Any]]:
    return [_task_view(t) for t in state.db.list_tasks()]


@app.post("/api/tasks")
async def create_task(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    assert state.scheduler
    for k in ("name", "prompt", "cron"):
        if not str(body.get(k) or "").strip():
            raise HTTPException(400, f"缺少 {k}")
    try:
        state.scheduler.validate_cron(body["cron"])
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    t = state.db.create_task(**body)
    state.scheduler.sync(t["id"])
    return _task_view(state.db.get_task(t["id"]) or t)


@app.put("/api/tasks/{tid}")
async def update_task(tid: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    assert state.scheduler
    if not state.db.get_task(tid):
        raise HTTPException(404, "任务不存在")
    if body.get("cron"):
        try:
            state.scheduler.validate_cron(body["cron"])
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
    t = state.db.update_task(tid, **body)
    state.scheduler.sync(tid)
    return _task_view(t or {})


@app.delete("/api/tasks/{tid}")
async def delete_task(tid: str) -> dict[str, Any]:
    state.db.delete_task(tid)
    if state.scheduler:
        state.scheduler.sync(tid)
    return {"ok": True}


@app.post("/api/tasks/{tid}/run")
async def run_task_now(tid: str) -> dict[str, Any]:
    assert state.scheduler
    if not state.db.get_task(tid):
        raise HTTPException(404, "任务不存在")
    sid = await state.scheduler.run_task(tid)
    return {"ok": True, "session_id": sid}


@app.get("/api/tasks/{tid}/runs")
async def task_runs(tid: str) -> list[dict[str, Any]]:
    return state.db.list_task_runs(tid)


@app.post("/api/cron/preview")
async def cron_preview(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    import datetime as dt

    try:
        trig = TaskScheduler.validate_cron(str(body.get("cron", "")))
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    cursor = dt.datetime.now().astimezone()
    times, prev = [], None
    for _ in range(3):
        nxt = trig.get_next_fire_time(prev, cursor)
        if not nxt:
            break
        times.append(nxt.strftime("%Y-%m-%d %H:%M"))
        prev, cursor = nxt, nxt + dt.timedelta(seconds=1)
    return {"ok": True, "next": times}


# ---------------------------------------------------------------------------
# files

def _kind(p: Path) -> str:
    ext = p.suffix.lower()
    if ext in ff.VIDEO_EXTS:
        return "video"
    if ext in ff.AUDIO_EXTS:
        return "audio"
    if ext in ff.IMAGE_EXTS:
        return "image"
    if ext in (".srt", ".vtt", ".ass", ".ssa"):
        return "subtitle"
    if ext in (".txt", ".md", ".json", ".csv", ".log", ".py", ".js", ".html", ".yaml", ".yml"):
        return "text"
    return "other"


def _resolve(path: str | None) -> Path:
    ws = state.settings.workspace
    if not path:
        return ws
    p = Path(path).expanduser()
    return (p if p.is_absolute() else ws / p).resolve()


@app.get("/api/files")
async def list_files(path: str = "") -> dict[str, Any]:
    base = _resolve(path)
    if not base.is_dir():
        raise HTTPException(404, "文件夹不存在")
    items = []
    for p in base.iterdir():
        if p.name.startswith(".") and p.name not in (".trash",):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        items.append({"name": p.name, "path": str(p), "is_dir": p.is_dir(), "size": st.st_size if p.is_file() else None,
                      "mtime": st.st_mtime, "kind": "dir" if p.is_dir() else _kind(p)})
    items.sort(key=lambda x: (not x["is_dir"], -x["mtime"]))
    ws = state.settings.workspace
    try:
        rel = base.relative_to(ws.resolve()).as_posix()
    except ValueError:
        rel = str(base)
    return {"path": str(base), "rel": "" if rel == "." else rel, "workspace": str(ws),
            "parent": str(base.parent) if base != ws.resolve() else None, "items": items}


@app.get("/api/files/raw")
async def raw_file(path: str) -> FileResponse:
    p = _resolve(path)
    if not p.is_file():
        raise HTTPException(404, "文件不存在")
    mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    if p.suffix.lower() in (".srt", ".ass", ".vtt", ".md", ".txt", ".log", ".json", ".csv"):
        mime = "text/plain; charset=utf-8"
    return FileResponse(p, media_type=mime, headers={"Cache-Control": "no-cache"})


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)) -> dict[str, Any]:
    dest_dir = state.settings.workspace / "uploads"
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in files:
        name = re.sub(r'[\\/:*?"<>|]+', "_", Path(f.filename or "file").name) or "file"
        dest = dest_dir / name
        n = 2
        while dest.exists():
            dest = dest_dir / f"{Path(name).stem}_{n}{Path(name).suffix}"
            n += 1
        with open(dest, "wb") as out:
            while chunk := await f.read(1 << 20):
                out.write(chunk)
        saved.append({"path": str(dest), "name": dest.name, "kind": _kind(dest), "size": dest.stat().st_size})
    return {"files": saved}


@app.post("/api/files/reveal")
async def reveal(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    p = _resolve(body.get("path"))
    if not p.exists():
        raise HTTPException(404, "不存在")
    import subprocess

    if ff.IS_WIN:
        subprocess.Popen(["explorer", "/select,", str(p)] if p.is_file() else ["explorer", str(p)])
    elif ff.IS_MAC:
        subprocess.Popen(["open", "-R", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p.parent if p.is_file() else p)])
    return {"ok": True}


# ---------------------------------------------------------------------------
# subtitle editor

@app.get("/api/subtitles")
async def get_subtitles(path: str) -> dict[str, Any]:
    p = _resolve(path)
    if not p.is_file():
        raise HTTPException(404, "字幕文件不存在")
    cues = sublib.load(p)
    media = None
    stem = p.name.split(".")[0]
    for cand in sorted(p.parent.iterdir()):
        if cand.suffix.lower() in ff.VIDEO_EXTS | ff.AUDIO_EXTS and cand.name.split(".")[0] == stem:
            media = str(cand)
            break
    return {"path": str(p), "cues": [c.to_dict(i) for i, c in enumerate(cues, 1)], "media": media,
            "has_words": sublib.load_words(p) is not None}


@app.put("/api/subtitles")
async def put_subtitles(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    p = _resolve(body.get("output") or body.get("path"))
    cues = [sublib.Cue(float(c["start"]), float(c["end"]), str(c.get("text", ""))) for c in body.get("cues") or []]
    sublib.save(p, cues)
    return {"ok": True, "path": str(p), "count": len(cues)}


# ---------------------------------------------------------------------------
# voice preview

PREVIEW_TEXT = "这个男人叫小帅，他万万没想到，一觉醒来，自己竟然成了全村最会剪视频的人。"


@app.post("/api/tts/preview")
async def tts_preview(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    tmp = state.settings.workspace / ".tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    out = tmp / f"preview_{int(time.time() * 1000)}.mp3"
    try:
        r = await synthesize(str(body.get("text") or PREVIEW_TEXT), out, state.settings.get(),
                             voice=body.get("voice") or None, engine=body.get("engine") or None)
    except TTSError as e:
        raise HTTPException(400, str(e)) from e
    return {"path": str(out), "engine": r["engine"], "errors": r.get("errors") or []}


# ---------------------------------------------------------------------------
# memory

@app.get("/api/memory")
async def get_memory() -> dict[str, Any]:
    return {"content": MEMORY_FILE.read_text("utf-8") if MEMORY_FILE.exists() else ""}


@app.put("/api/memory")
async def put_memory(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    MEMORY_FILE.write_text(str(body.get("content", "")), "utf-8")
    return {"ok": True}


@app.post("/api/workspace/clean-tmp")
async def clean_tmp() -> dict[str, Any]:
    ws = state.settings.workspace
    freed = 0
    for d in (ws / ".tmp", ws / ".frames"):
        if d.exists():
            freed += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            shutil.rmtree(d, ignore_errors=True)
    return {"ok": True, "freed_mb": round(freed / 1048576, 1)}
