"""The agent loop: model turn → tool calls → tool results → repeat."""
from __future__ import annotations

import asyncio
import json
import logging
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .. import state
from ..media.ffmpeg import IMAGE_EXTS, FFmpegError
from ..providers import ProviderError, make_provider, stream_retrying
from ..tools import REGISTRY, ToolContext, ToolError, call_tool, to_text
from .prompts import build_system_prompt
from .runs import Run

log = logging.getLogger("autostudio.agent")

MAX_CONTEXT_CHARS = 350_000
KEEP_IMAGES = 4      # messages whose images are still sent
MAX_IMAGES = 8       # and no more than this many images in one request
DANGEROUS_PERMISSION = {"run_command": "shell", "social_publish": "publish", "studio_run": "code"}


def prepare_history(history: list[dict[str, Any]], vision: bool) -> list[dict[str, Any]]:
    msgs = [dict(m) for m in history]
    # 1) close dangling tool calls (e.g. the user pressed stop mid-tool)
    fixed: list[dict[str, Any]] = []
    for i, m in enumerate(msgs):
        fixed.append(m)
        if m["role"] == "assistant" and m.get("tool_calls"):
            answered = set()
            j = i + 1
            while j < len(msgs) and msgs[j]["role"] == "tool":
                answered.add(msgs[j].get("tool_call_id"))
                j += 1
            for tc in m["tool_calls"]:
                if tc["id"] not in answered:
                    fixed.append({"role": "tool", "tool_call_id": tc["id"], "name": tc["name"],
                                  "content": "（该工具调用已被用户中断，未完成）"})
    msgs = fixed
    # 2) keep only the most recent images
    seen = kept = 0
    for m in reversed(msgs):
        if m.get("images"):
            if not vision:
                m["content"] = (m.get("content") or "") + "\n（附带的图片未发送：当前模型不支持图片输入）"
                m["images"] = []
            elif seen >= KEEP_IMAGES or kept >= MAX_IMAGES:
                m["images"] = []
            else:
                m["images"] = m["images"][:MAX_IMAGES - kept]
                seen += 1
                kept += len(m["images"])
    # 3) shrink old tool outputs if the context is huge
    total = sum(len(m.get("content") or "") for m in msgs)
    if total > MAX_CONTEXT_CHARS:
        for m in msgs[:-12]:
            if m["role"] == "tool" and len(m.get("content") or "") > 400:
                total -= len(m["content"]) - 60
                m["content"] = m["content"][:200] + "…（较早的工具输出已省略）"
                if total <= MAX_CONTEXT_CHARS:
                    break
    return msgs


def _contiguous_tools(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure tool results directly follow the assistant message that requested them."""
    out: list[dict[str, Any]] = []
    i = 0
    while i < len(history):
        m = history[i]
        out.append(m)
        i += 1
        if m["role"] == "assistant" and m.get("tool_calls"):
            ids = {tc["id"] for tc in m["tool_calls"]}
            tools = []
            while i < len(history) and history[i]["role"] == "tool":
                tools.append(history[i])
                i += 1
            out.extend(t for t in tools if t.get("tool_call_id") in ids)
    return out


async def execute_tool(run: Run, tc: Any, settings: dict[str, Any], workspace: Path, headless: bool,
                       allow_dangerous: bool, auto_publish: bool = False) -> dict[str, Any]:
    t = REGISTRY.get(tc.name)
    try:
        args = json.loads(tc.arguments or "{}")
        if not isinstance(args, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        args = None
    await run.emit({"type": "tool_start", "id": tc.id, "name": tc.name, "arguments": args if args is not None else tc.arguments})
    msg: dict[str, Any] = {"role": "tool", "tool_call_id": tc.id, "name": tc.name, "arguments": args, "ok": False}
    if t is None:
        msg["content"] = f"错误：不存在名为 {tc.name} 的工具。"
        return msg
    if args is None:
        msg["content"] = "错误：工具参数不是合法的 JSON 对象，请重新调用。"
        return msg
    publish_mode = "now"
    if t.dangerous:
        perm_key = DANGEROUS_PERMISSION.get(t.name, "shell")
        perm = (settings.get("permissions") or {}).get(perm_key, "ask")
        if perm_key == "publish" and (headless or args.get("queue_only")):
            # Unattended runs never publish unseen: posts wait in the review queue unless the task opted in.
            allowed = True
            publish_mode = "now" if headless and auto_publish and not args.get("queue_only") else "queue"
        elif headless:
            allowed = allow_dangerous
        elif perm == "allow":
            allowed = True
        elif perm == "deny":
            allowed = False
        else:
            remembered = perm_key in (state.runs.session_allow.get(run.session_id) or set()) if state.runs else False
            if remembered:
                allowed = True
            else:
                answer = await run.request_approval(tc.id, t.name, args, can_remember=perm_key in ("code", "shell"))
                if answer == "session" and state.runs:
                    state.runs.session_allow.setdefault(run.session_id, set()).add(perm_key)
                allowed = bool(answer)
        if not allowed:
            msg["content"] = "用户未批准执行此操作（或设置中禁用了该类操作）。请换一种方式，或向用户说明需要什么权限。"
            msg["denied"] = True
            return msg
    ctx = ToolContext(session_id=run.session_id, workspace=workspace, emit=run.emit, call_id=tc.id,
                      settings=settings, headless=headless, publish_mode=publish_mode)
    try:
        result = await call_tool(t, ctx, args)
        ok = not (isinstance(result, dict) and result.get("ok") is False)
    except asyncio.CancelledError:
        ctx.cleanup()
        raise
    except (ToolError, FFmpegError, FileNotFoundError, ValueError, PermissionError) as e:
        result, ok = {"ok": False, "error": str(e)}, False
    except Exception as e:  # noqa: BLE001
        log.error("tool %s crashed: %s", t.name, traceback.format_exc())
        result, ok = {"ok": False, "error": f"{type(e).__name__}: {e}"}, False
    finally:
        ctx.cleanup()
    files: list[str] = []
    images: list[str] = []
    editor = None
    if isinstance(result, dict):
        files = [str(f) for f in result.pop("files", []) or []]
        images = [str(f) for f in result.pop("images", []) or []]
        editor = result.pop("editor", None)
    msg.update(content=to_text(result), ok=ok, files=files, images=images)
    if editor:
        msg["editor"] = editor
    return msg


async def run_agent(run: Run, session_id: str, text: str, attachments: list[str] | None = None, *,
                    headless: bool = False, allow_dangerous: bool = False, auto_publish: bool = False) -> str:
    settings = state.settings.get()
    session = state.db.get_session(session_id)
    if not session:
        raise RuntimeError("对话不存在")
    active = settings.get("active") or {}
    pid = session.get("provider") or active.get("provider")
    model = session.get("model") or (active.get("model") if pid == active.get("provider") else None)
    provider = make_provider(settings, pid, model, session_id=session_id)
    model_name = getattr(provider, "model", model or "")
    workspace = state.settings.workspace

    attachments = attachments or []
    content = text
    if attachments:
        rels = []
        for a in attachments:
            p = Path(a)
            try:
                rels.append(p.resolve().relative_to(workspace.resolve()).as_posix())
            except ValueError:
                rels.append(str(p))
        content += "\n\n[用户附件]\n" + "\n".join(f"- {r}" for r in rels)
    images = [a for a in attachments if Path(a).suffix.lower() in IMAGE_EXTS]
    user_msg = {"role": "user", "content": content, "images": images, "attachments": attachments, "text": text}
    state.db.add_message(session_id, user_msg)
    await run.emit({"type": "user_message", "message": user_msg})
    if session.get("title") in ("新对话", "", None) and session.get("kind") == "chat":
        title = text.strip().splitlines()[0][:40] if text.strip() else "新对话"
        state.db.update_session(session_id, title=title)
        await run.emit({"type": "title", "title": title})

    history = [m for m in state.db.get_messages(session_id)]
    tools = [t.schema() for t in REGISTRY.values()]
    system = build_system_prompt(workspace, f"{pid} / {model_name}", provider.vision,
                                 (settings.get("agent") or {}).get("extra_prompt", ""), headless)
    max_steps = int((settings.get("agent") or {}).get("max_steps") or 60)
    final_text = ""

    for _step in range(max_steps):
        partial = {"text": "", "reasoning": ""}

        async def emit(ev: dict[str, Any]) -> None:
            if ev["type"] == "text_delta":
                partial["text"] += ev["delta"]
            elif ev["type"] == "reasoning_delta":
                partial["reasoning"] += ev["delta"]
            await run.emit(ev)

        async def on_retry(n: int, _e: Exception) -> None:
            # a dropped connection only loses this one step, so the half answer on screen is replaced
            partial["text"] = partial["reasoning"] = ""
            await run.emit({"type": "assistant_retry", "text": f"与模型的连接中断，正在重新生成这一步（第 {n} 次重试）"})

        await run.emit({"type": "assistant_start", "model": model_name})
        try:
            result = await stream_retrying(provider, system,
                                           prepare_history(_contiguous_tools(history), provider.vision),
                                           tools, emit, on_retry)
        except asyncio.CancelledError:
            if partial["text"].strip():
                state.db.add_message(session_id, {"role": "assistant", "content": partial["text"] + "\n\n（已停止）",
                                                  "reasoning": partial["reasoning"], "model": model_name})
            raise
        except PermissionError as e:
            raise ProviderError(str(e)) from e
        assistant = {
            "role": "assistant",
            "content": result.text,
            "tool_calls": [asdict(tc) for tc in result.tool_calls],
            "reasoning": result.reasoning,
            "model": model_name,
        }
        if result.reasoning_content:
            assistant["reasoning_content"] = result.reasoning_content
        if result.usage:
            assistant["usage"] = result.usage
        state.db.add_message(session_id, assistant)
        history.append(assistant)
        await run.emit({"type": "assistant_done", "message": assistant})
        if result.text.strip():
            final_text = result.text
        if not result.tool_calls:
            break
        for tc in result.tool_calls:
            tool_msg = await execute_tool(run, tc, settings, workspace, headless, allow_dangerous, auto_publish)
            state.db.add_message(session_id, tool_msg)
            history.append(tool_msg)
            await run.emit({"type": "tool_result", "message": tool_msg})
    else:
        await run.emit({"type": "notice", "text": f"已达到单次最大步数（{max_steps}），可以发送“继续”让我接着做。"})
    return final_text
