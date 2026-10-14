"""Record histories with an OLDER checkout, so the replay test proves today's workflows still replay what a
worker built before a patched() change recorded.

    git worktree add ../aoa-old <commit-before-the-change>
    PYTHONPATH=../aoa-old .venv/bin/python tests/histories/capture_legacy.py

Used for: legacy_session_pre_timers.json and legacy_poll_pre_search_attrs.json (commit 20e1c8b, before the digest /
approval timers and PR search attributes). Only APIs that existed then are used.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

import aoa
from aoa.activities import AgentActivities
from aoa.config import Settings
from aoa.core import AgentCore
from aoa.delivery import deliver_event, workflow_cfg
from aoa.events import Event, user_event
from aoa.memory import MemoryStore
from aoa.store import Store
from aoa.testing import FakeLLM, MemoryScheduler, text, tool
from aoa.workflows import (
    GithubPollWorkflow, HelperWorkflow, MergePrWorkflow, PrReviewWorkflow, RoutineWorkflow, SessionWorkflow,
)

HERE = Path(__file__).parent
PR = {"repo": "agileresearchservices/demo", "number": 7, "sha": "a" * 40, "title": "Add thing", "author": "agileresearchservices"}
VERDICT = json.dumps({"verdict": "approve", "summary": "Looks fine.", "findings": []})


def stub(name, result):
    @activity.defn(name=name)
    async def _a(arg=None):
        return result
    return _a


async def save(client, name, wid):
    hist = await client.get_workflow_handle(wid).fetch_history()
    (HERE / f"{name}.json").write_text(json.dumps({"workflow_id": wid, "history": json.loads(hist.to_json())}, indent=1))
    print(f"saved {name}.json ({len(hist.events)} events) from {Path(aoa.__file__).parent}")


def worker(client, s, llm, stubs):
    core = AgentCore(s, Store(s.db_path), MemoryStore(s.memory_dir), llm, MemoryScheduler())
    names = {s_.__temporal_activity_definition.name for s_ in stubs}
    real = [a for a in AgentActivities(core, client, s).all() if a.__temporal_activity_definition.name not in names]
    flows = [SessionWorkflow, HelperWorkflow, RoutineWorkflow, GithubPollWorkflow, PrReviewWorkflow, MergePrWorkflow]
    return Worker(client, task_queue=s.task_queue, workflows=flows, activities=real + stubs), core.store


def env():
    os.environ.update(AOA_DATA_DIR=tempfile.mkdtemp(prefix="aoa-legacy-"), AOA_TASK_QUEUE=f"aoa-legacy-{uuid.uuid4().hex[:6]}",
                      AOA_BATCH_WINDOW="0.2")
    return Settings.from_env()


async def main():
    client = await Client.connect(os.environ.get("AOA_TEMPORAL_ADDRESS", "localhost:7233"))
    s = env()
    w, store = worker(client, s, FakeLLM([[tool("send_user", kind="update", text="fyi")], text("ok"),
                                          [tool("send_user", text="pong")], text("done")]), [])
    sid = f"leg-{uuid.uuid4().hex[:6]}"
    async with w:
        await deliver_event(client, s, sid, Event(source="webhook", text="news", dedupe_key=f"w:{sid}").to_dict())
        await deliver_event(client, s, sid, user_event("ping").to_dict())
        for _ in range(100):
            if any(o["payload"].get("text") == "pong" for o in store.outbox_since(sid)):
                break
            await asyncio.sleep(0.2)
        h = client.get_workflow_handle(f"session-{sid}")
        await h.signal("stop")
        await asyncio.wait_for(h.result(), 30)
    await save(client, "legacy_session_pre_timers", f"session-{sid}")

    s = env()
    w, _ = worker(client, s, FakeLLM([text(VERDICT)]),
                  [stub("poll_github", {"announced": 1, "review": [PR]}), stub("publish_pr_review", {"posted": True})])
    async with w:
        wid = f"poll-{uuid.uuid4().hex[:6]}"
        await client.execute_workflow("GithubPollWorkflow", {"session": "main", "user": "agileresearchservices", "cfg": workflow_cfg(s)},
                                      id=wid, task_queue=s.task_queue)
        await client.get_workflow_handle(f"pr-review-main-{PR['repo'].replace('/', '-')}-{PR['number']}-{PR['sha'][:12]}").result()
    await save(client, "legacy_poll_pre_search_attrs", wid)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
