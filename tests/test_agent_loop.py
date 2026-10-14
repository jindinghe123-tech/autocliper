"""The agent loop survives a connection that drops in the middle of a model answer."""
import asyncio
import os
import ssl
import tempfile

os.environ.setdefault("AUTOSTUDIO_HOME", tempfile.mkdtemp(prefix="autostudio_test_"))

import httpx  # noqa: E402

from autostudio import state  # noqa: E402
from autostudio.agent import loop  # noqa: E402
from autostudio.agent.runs import Run  # noqa: E402
from autostudio.providers.base import TurnResult  # noqa: E402


class FlakyProvider:
    model = "fake"
    vision = False

    def __init__(self):
        self.calls = 0

    async def stream(self, system, messages, tools, emit):
        self.calls += 1
        await emit({"type": "text_delta", "delta": "半句话"})
        if self.calls == 1:
            raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")
        if self.calls == 2:
            raise ssl.SSLError(1, "[SSL: SSLV3_ALERT_BAD_RECORD_MAC] sslv3 alert bad record mac")
        return TurnResult(text="完整的回答")


def test_dropped_stream_is_generated_again(monkeypatch):
    prov = FlakyProvider()
    monkeypatch.setattr(loop, "make_provider", lambda *a, **k: prov)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *_: real_sleep(0))
    sid = state.db.create_session(provider="x", model="fake")["id"]
    run = Run(sid)
    assert asyncio.run(loop.run_agent(run, sid, "你好")) == "完整的回答"
    assert prov.calls == 3
    assert sum(e["type"] == "assistant_retry" for e in run.events) == 2
    saved = [m for m in state.db.get_messages(sid) if m["role"] == "assistant"]
    assert [m["content"] for m in saved] == ["完整的回答"]          # the broken half answer is not kept
