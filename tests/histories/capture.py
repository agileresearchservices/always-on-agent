"""Record workflow histories for the replay test (tests/test_replay.py).

    .venv/bin/python tests/histories/capture.py [--only NAME ...]

Needs a local Temporal server (the launchd one is fine; it uses its own task queue and throwaway data dir).
Model calls are scripted (FakeLLM) and the GitHub-facing activities are replaced by stubs, so nothing leaves the
machine. Re-record only to ADD a scenario for a new code path. Never re-record an existing file to make a failing
replay pass: a failure means in-flight workflows would break, so guard the change with workflow.patched().
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

from aoa.activities import AgentActivities
from aoa.config import Settings
from aoa.core import AgentCore
from aoa.delivery import deliver_event, workflow_cfg
from aoa.events import Event, user_event
from aoa.memory import MemoryStore
from aoa.search_attrs import ensure_registered, pr_attrs
from aoa.store import Store
from aoa.testing import FakeLLM, text, tool
from aoa.testing import MemoryScheduler
from aoa.workflows import (
    GithubPollWorkflow, HelperWorkflow, MergePrWorkflow, PrReviewWorkflow, RoutineWorkflow, SessionWorkflow,
)

HERE = Path(__file__).parent
PR = {"repo": "agileresearchservices/demo", "number": 7, "sha": "a" * 40, "title": "Add thing", "author": "agileresearchservices"}
VERDICT = json.dumps({"verdict": "approve", "summary": "Looks fine.", "findings": []})
GH_STUBBED = {"poll_github", "publish_pr_review", "merge_inspect", "merge_pr", "merge_update_branch",
              "merge_delete_branch", "merge_issue_states", "resolve_conflicts", "merge_report"}


def act_name(fn: Any) -> str:
    return fn.__temporal_activity_definition.name


def stub(name: str, results: Any):
    """An activity that returns canned results in order (the last one repeats)."""
    seq = list(results) if isinstance(results, list) else [results]

    @activity.defn(name=name)
    async def _a(arg: Any = None) -> Any:
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return _a


def mk_env() -> Settings:
    os.environ.update(
        AOA_DATA_DIR=tempfile.mkdtemp(prefix="aoa-capture-"), AOA_TASK_QUEUE=f"aoa-capture-{uuid.uuid4().hex[:6]}",
        AOA_BATCH_WINDOW="0.2", AOA_MIN_UPDATE_INTERVAL="3", AOA_QUIET_START="0", AOA_QUIET_END="0",
        AOA_APPROVAL_NUDGE="6", AOA_APPROVAL_EXPIRE="9",
    )
    return Settings.from_env()


async def save(client: Client, name: str, workflow_id: str) -> None:
    hist = await client.get_workflow_handle(workflow_id).fetch_history()
    (HERE / f"{name}.json").write_text(json.dumps({"workflow_id": workflow_id, "history": json.loads(hist.to_json())}, indent=1))
    print(f"saved {name}.json ({len(hist.events)} events)")


async def wait_for(pred, timeout: float = 40, show=None):
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        if pred():
            return
        await asyncio.sleep(0.2)
    raise TimeoutError(f"scenario did not finish: {show() if show else ''}")


async def worker_with(client: Client, s: Settings, llm: FakeLLM, stubs: list) -> tuple[Worker, Store]:
    store = Store(s.db_path)
    core = AgentCore(s, store, MemoryStore(s.memory_dir), llm, MemoryScheduler())
    acts = AgentActivities(core, client, s)
    real = [a for a in acts.all() if act_name(a) not in GH_STUBBED]
    flows = [SessionWorkflow, HelperWorkflow, RoutineWorkflow, GithubPollWorkflow, PrReviewWorkflow, MergePrWorkflow]
    return Worker(client, task_queue=s.task_queue, workflows=flows, activities=real + stubs), store


async def session(client: Client) -> None:
    """A background turn that sends an update, holds a second (digest timer), files an approval that is nudged and
    expires (approval timers), then a person's message that gets a reply."""
    s = mk_env()
    script = [
        [tool("send_user", kind="update", text="first")], [tool("send_user", kind="update", text="second")],
        [tool("call_deferred", name="external_send", arguments={"channel": "email:bob", "text": "hi"})],
        text("waiting for the approval"),
        [tool("send_user", text="pong")], text("done"),
    ]
    w, store = await worker_with(client, s, FakeLLM(script), [])
    sid = f"cap-{uuid.uuid4().hex[:6]}"
    async with w:
        await deliver_event(client, s, sid, Event(source="webhook", text="news", dedupe_key=f"w:{sid}").to_dict())
        await wait_for(lambda: any(a for a in [store.approval_get(o["payload"]["approval_id"]) for o in store.outbox_since(sid) if o["kind"] == "approval"] if a["status"] == "expired")
                       and any("Held updates" in o["payload"].get("text", "") for o in store.outbox_since(sid)),
                       show=lambda: [(o["kind"], o["payload"].get("text", "")[:60]) for o in store.outbox_since(sid)])
        await deliver_event(client, s, sid, user_event("ping").to_dict())
        await wait_for(lambda: any(o["payload"].get("text") == "pong" for o in store.outbox_since(sid)))
        handle = client.get_workflow_handle(f"session-{sid}")
        await handle.signal("stop")
        await asyncio.wait_for(handle.result(), 30)
    await save(client, "session_timers_and_reply", f"session-{sid}")


async def poll_and_review(client: Client) -> None:
    """GithubPollWorkflow starts a PrReviewWorkflow child (with search attributes), which runs the reviewer helper."""
    s = mk_env()
    stubs = [stub("poll_github", {"announced": 1, "review": [PR], "search_attrs": True}), stub("publish_pr_review", {"posted": True, "verdict": "approve"})]
    w, _ = await worker_with(client, s, FakeLLM([text(VERDICT)]), stubs)
    async with w:
        wid = f"poll-{uuid.uuid4().hex[:6]}"
        await client.execute_workflow("GithubPollWorkflow", {"session": "main", "user": "agileresearchservices", "cfg": workflow_cfg(s)},
                                      id=wid, task_queue=s.task_queue)
        child = f"pr-review-main-{PR['repo'].replace('/', '-')}-{PR['number']}-{PR['sha'][:12]}"
        await client.get_workflow_handle(child).result()
    await save(client, "github_poll", wid)
    await save(client, "pr_review", child)


def inspect(action: str, **kw: Any) -> dict[str, Any]:
    base = {"action": action, "reason": action, "title": "Add thing", "head_ref": "feat", "base_ref": "main", "same_repo": True,
            "default_base": True, "protected_refs": ["main"], "merge_sha": None, "issues": [{"number": 3, "state": "OPEN"}]}
    return base | kw


async def merge(client: Client, name: str, stubs: list) -> None:
    s = mk_env()
    w, _ = await worker_with(client, s, FakeLLM([]), stubs + [stub("merge_report", None)])
    offer = {"repo": PR["repo"], "number": PR["number"], "sha": PR["sha"], "verdict": "approve", "card_text": "card"}
    async with w:
        wid = f"merge-main-{name}-{uuid.uuid4().hex[:6]}"
        await client.execute_workflow(
            "MergePrWorkflow", {"session": "main", "offer_id": "mrg-0123456789ab", "offer": offer, "card_ts": "1.0",
                                "poll_seconds": 1, "issue_poll_seconds": 1},
            id=wid, task_queue=s.task_queue, search_attributes=pr_attrs(PR["repo"], PR["number"], PR["sha"], "main"))
    await save(client, name, wid)


SCENARIOS = {
    "session_timers_and_reply": session,
    "github_poll": poll_and_review,
    "merge_wait_then_merge": lambda c: merge(c, "merge_wait_then_merge", [
        stub("merge_inspect", [inspect("wait"), inspect("merge")]), stub("merge_pr", {"merged": True, "merge_sha": "b" * 40}),
        stub("merge_delete_branch", {"ref": "feat", "result": "deleted"}),
        stub("merge_issue_states", [[{"number": 3, "state": "CLOSED"}]])]),
    "merge_update_branch": lambda c: merge(c, "merge_update_branch", [
        stub("merge_inspect", inspect("update_branch")), stub("merge_update_branch", {"ok": True})]),
    "merge_conflict_resolved": lambda c: merge(c, "merge_conflict_resolved", [
        stub("merge_inspect", inspect("conflict")),
        stub("resolve_conflicts", {"status": "resolved", "new_sha": "c" * 40, "files": ["a.py"]})]),
}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", choices=sorted(SCENARIOS) + ["pr_review"])
    args = ap.parse_args()
    client = await Client.connect(os.environ.get("AOA_TEMPORAL_ADDRESS", "localhost:7233"))
    await ensure_registered(client)
    for name, fn in SCENARIOS.items():
        if not args.only or name in args.only or (name == "github_poll" and "pr_review" in args.only):
            await fn(client)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
