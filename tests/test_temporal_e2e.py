"""End-to-end tests against a real Temporal server.

They skip automatically when no server answers on AOA_TEMPORAL_ADDRESS (default localhost:7233).
Start one with:   temporal server start-dev
Then:             pytest -m temporal
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from temporalio.client import Client

from aoa.config import Settings
from aoa.core import AgentCore
from aoa.delivery import deliver_event
from aoa.events import user_event
from aoa.memory import MemoryStore
from aoa.scheduling import TemporalScheduler
from aoa.store import Store
from aoa.testing import FakeLLM, text, tool
from aoa.worker import build_worker

pytestmark = pytest.mark.temporal


@pytest.fixture
async def client():
    addr = os.environ.get("AOA_TEMPORAL_ADDRESS", "localhost:7233")
    try:
        return await asyncio.wait_for(Client.connect(addr), timeout=3)
    except Exception:  # noqa: BLE001
        pytest.skip(f"no Temporal server at {addr}; run `temporal server start-dev`")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AOA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AOA_TASK_QUEUE", f"aoa-test-{uuid.uuid4().hex[:6]}")
    monkeypatch.setenv("AOA_BATCH_WINDOW", "0.2")
    return Settings.from_env()


async def _run(client, s, script, session, events, expect_kinds, timeout=30):
    store = Store(s.db_path)
    llm = FakeLLM(script)
    core = AgentCore(s, store, MemoryStore(s.memory_dir), llm, TemporalScheduler(client, s))
    worker = await build_worker(s, client, core)
    async with worker:
        for ev in events:
            await deliver_event(client, s, session, ev.to_dict())
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            kinds = [o["kind"] for o in store.outbox_since(session)]
            if len(kinds) >= expect_kinds:
                break
            await asyncio.sleep(0.2)
        await client.get_workflow_handle(f"session-{session}").signal("stop")
    return store, llm


async def test_user_message_gets_reply(client, env):
    sid = f"t-{uuid.uuid4().hex[:6]}"
    store, llm = await _run(
        client, env, [[tool("send_user", text="pong")], text("done")], sid, [user_event("ping")], 1
    )
    assert store.outbox_since(sid)[0]["payload"]["text"] == "pong"
    assert len(llm.calls) == 2


async def test_transient_model_failure_retries_without_replaying_steps(client, env):
    sid = f"t-{uuid.uuid4().hex[:6]}"
    script = [
        [tool("memory_search", query="x")],
        RuntimeError("provider 529"),            # step 2 fails once, Temporal retries the activity
        [tool("send_user", text="recovered")],
        text("done"),
    ]
    store, llm = await _run(client, env, script, sid, [user_event("go")], 1, timeout=60)
    assert store.outbox_since(sid)[0]["payload"]["text"] == "recovered"
    roles = [m["role"] for m in store.load_messages(sid)]
    assert roles == ["user", "assistant", "user", "assistant", "user", "assistant"]  # no duplicates
    assert len(llm.calls) == 4  # the first step was NOT re-run after the retry


async def test_helper_child_workflow_returns_only_result(client, env):
    sid = f"t-{uuid.uuid4().hex[:6]}"

    def helper_or_main(call):
        first = call["messages"][0]["content"][0]["text"]
        if first.startswith("Task from the main agent"):
            return text("HELPER RESULT")
        return [tool("send_user", text="got: " + call["messages"][-1]["content"][0]["content"])]

    script = [[tool("spawn_helper", task="look it up")], helper_or_main, helper_or_main, text("done")]
    store, _ = await _run(client, env, script, sid, [user_event("research")], 1, timeout=60)
    assert store.outbox_since(sid)[0]["payload"]["text"] == "got: HELPER RESULT"
    assert len(store.load_messages(sid)) == 6  # helper turns live in their own conversation
