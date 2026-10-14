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
        handle = client.get_workflow_handle(f"session-{session}")
        await handle.signal("stop")
        # let the in-flight turn finish before the worker shuts down (the stop is seen between turns)
        await asyncio.wait_for(handle.result(), timeout=timeout)
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


async def test_poll_starts_one_review_per_commit_and_publishes(client, env, monkeypatch):
    """Poll -> PrReviewWorkflow -> reviewer HelperWorkflow (scripted) -> GitHub comment review + Slack card."""
    import httpx

    from aoa import github_monitor
    from aoa.delivery import workflow_cfg

    monkeypatch.setenv("AOA_GITHUB_USER", "agileresearchservices")
    monkeypatch.setenv("GITHUB_TOKEN", "fake")
    sid = f"t-{uuid.uuid4().hex[:6]}"
    pr = {"repo": "agileresearchservices/demo", "number": 7, "title": "Tidy", "author": "agileresearchservices",
          "url": "https://github.com/agileresearchservices/demo/pull/7", "sha": "abc123def4567890", "why": "new"}

    async def fake_poll(http, store, session, user, token):
        return [pr]

    posts = []

    async def fake_gh(method, path, json=None, accept=""):
        posts.append((method, path, json))
        return httpx.Response(200, json={})

    monkeypatch.setattr("aoa.activities.poll_once", fake_poll)
    monkeypatch.setattr(github_monitor, "gh_call", fake_gh)
    verdict = '{"verdict": "request_changes", "summary": "Bug.", "findings": [{"severity": "high", "file": "a.py", "line": 2, "comment": "off by one"}]}'
    store = Store(env.db_path)
    llm = FakeLLM([text(verdict)])  # the reviewer answers in one step
    core = AgentCore(env, store, MemoryStore(env.memory_dir), llm, TemporalScheduler(client, env))
    worker = await build_worker(env, client, core)
    async with worker:
        inp = {"session": sid, "user": "agileresearchservices", "cfg": workflow_cfg(env)}
        await client.execute_workflow("GithubPollWorkflow", inp, id=f"poll-{sid}", task_queue=env.task_queue)
        await client.execute_workflow("GithubPollWorkflow", inp, id=f"poll2-{sid}", task_queue=env.task_queue)
        deadline = asyncio.get_event_loop().time() + 30
        while asyncio.get_event_loop().time() < deadline and not any(o["kind"] == "pr_review" for o in store.outbox_since(sid)):
            await asyncio.sleep(0.2)
    cards = [o for o in store.outbox_since(sid) if o["kind"] == "pr_review"]
    assert len(cards) == 1 and cards[0]["payload"]["verdict"] == "request_changes"
    assert len(llm.calls) == 1  # second poll found the same commit: no second review
    assert posts == [("POST", "/repos/agileresearchservices/demo/pulls/7/reviews",
                      {"body": posts[0][2]["body"], "event": "COMMENT", "commit_id": pr["sha"]})]
    assert "off by one" in posts[0][2]["body"]


async def test_merge_workflow_waits_for_checks_merges_and_closes_out(client, env, monkeypatch):
    """[Merge] click -> MergePrWorkflow: wait while a check runs, squash-merge pinned to the reviewed commit,
    delete the branch, see the linked issue close, report to the outbox for the card."""
    import httpx

    from aoa import github_monitor

    monkeypatch.setenv("AOA_GITHUB_USER", "agileresearchservices")
    sid, sha, repo = f"t-{uuid.uuid4().hex[:6]}", "abc123def4567890", "agileresearchservices/demo"
    offer = {"repo": repo, "number": 7, "sha": sha, "title": "Tidy", "url": "https://github.com/x/pull/7",
             "verdict": "approve", "card_text": "card"}
    pr = {"state": "open", "merged": False, "draft": False, "mergeable": True, "mergeable_state": "clean",
          "title": "Tidy", "head": {"sha": sha, "ref": "feat/x", "repo": {"full_name": repo}},
          "base": {"ref": "main", "repo": {"full_name": repo, "default_branch": "main"}}}
    seen = {"runs": 0, "issue": 0}
    calls = []

    async def fake_gh(method, path, json=None, accept=""):
        calls.append((method, path))
        if path.endswith("/check-runs?per_page=100"):
            seen["runs"] += 1
            st = "in_progress" if seen["runs"] == 1 else "completed"
            body = {"check_runs": [{"name": "test", "status": st, "conclusion": "success" if st == "completed" else None}]}
        elif path.endswith("/status"):
            body = {"state": "pending", "total_count": 0}
        elif path == "/graphql":
            body = {"data": {"repository": {"pullRequest": {"closingIssuesReferences": {"nodes": [
                {"number": 3, "state": "OPEN", "repository": {"nameWithOwner": repo}}]}}}}}
        elif path.endswith("/issues/3"):
            seen["issue"] += 1
            body = {"state": "open" if seen["issue"] == 1 else "closed"}
        elif method == "PUT" and path.endswith("/merge"):
            assert json == {"merge_method": "squash", "sha": sha, "commit_title": "Tidy (#7)"}
            body = {"merged": True, "sha": "m3rg3d0000"}
        else:
            body = pr if method == "GET" else None
        return httpx.Response(200, json=body)

    monkeypatch.setattr(github_monitor, "gh_call", fake_gh)
    store = Store(env.db_path)
    core = AgentCore(env, store, MemoryStore(env.memory_dir), FakeLLM([]), TemporalScheduler(client, env))
    async with await build_worker(env, client, core):
        out = await client.execute_workflow(
            "MergePrWorkflow",
            {"session": sid, "offer_id": "mrg-0123456789ab", "offer": offer, "card_ts": "9.9",
             "poll_seconds": 0.2, "issue_poll_seconds": 0.2},
            id=f"merge-{sid}", task_queue=env.task_queue,
        )
    assert out["status"] == "merged" and out["merge_sha"] == "m3rg3d0000"
    assert out["branch"] == {"ref": "feat/x", "result": "deleted"}
    assert out["issues"] == [{"repo": repo, "number": 3, "state": "CLOSED"}]
    assert seen["runs"] == 2  # waited once for the running check
    assert [c for c in calls if c[0] in ("PUT", "DELETE")] == [
        ("PUT", f"/repos/{repo}/pulls/7/merge"), ("DELETE", f"/repos/{repo}/git/refs/heads/feat/x")]
    (item,) = store.outbox_since(sid)
    assert item["kind"] == "pr_merge" and item["payload"]["card_ts"] == "9.9" and not item["payload"]["retry"]
    assert "Merged" in item["payload"]["text"] and "demo#3 closed" in item["payload"]["text"]


async def test_merge_workflow_with_conflicts_resolves_and_marks_the_new_commit(client, env, monkeypatch):
    """Conflict -> resolve_conflicts (resolver faked; the git side is tested in test_pr_resolve.py) -> report
    'resolved' with no retry, and the new commit is marked so its review is told about the resolution."""
    import httpx

    from aoa import activities, github_monitor

    monkeypatch.setenv("AOA_GITHUB_USER", "agileresearchservices")
    monkeypatch.setenv("GITHUB_TOKEN", "fake")
    sid, sha, repo = f"t-{uuid.uuid4().hex[:6]}", "abc123def4567890", "agileresearchservices/demo"
    offer = {"repo": repo, "number": 7, "sha": sha, "title": "Tidy", "url": "https://github.com/x/pull/7",
             "verdict": "approve", "card_text": "card"}
    pr = {"state": "open", "merged": False, "draft": False, "mergeable": False, "mergeable_state": "dirty",
          "title": "Tidy", "head": {"sha": sha, "ref": "feat/x", "repo": {"full_name": repo}},
          "base": {"ref": "main", "repo": {"full_name": repo, "default_branch": "main"}}}

    async def fake_gh(method, path, json=None, accept=""):
        assert method == "GET", (method, path)  # nothing is merged or deleted
        body = {"check_runs": []} if "check-runs" in path else {"total_count": 0} if path.endswith("/status") else pr
        return httpx.Response(200, json=body)

    seen = {}

    class FakeResolver:
        def __init__(self, **kw):
            seen.update(kw)

        async def run(self):
            return {"status": "resolved", "new_sha": "n3wsha0000", "files": ["app.py"], "summaries": {}, "tests": "passed"}

    monkeypatch.setattr(github_monitor, "gh_call", fake_gh)
    monkeypatch.setattr(activities, "Resolver", FakeResolver)
    store = Store(env.db_path)
    core = AgentCore(env, store, MemoryStore(env.memory_dir), FakeLLM([]), TemporalScheduler(client, env))
    async with await build_worker(env, client, core):
        out = await client.execute_workflow(
            "MergePrWorkflow", {"session": sid, "offer_id": "mrg-0123456789ab", "offer": offer, "card_ts": "9.9"},
            id=f"merge-{sid}", task_queue=env.task_queue,
        )
    assert out["status"] == "resolved" and seen["sha"] == sha and seen["head_ref"] == "feat/x" and seen["base_ref"] == "main"
    assert seen["token"] == "fake" and seen["remote_url"] == f"https://github.com/{repo}.git"
    assert store.kv_get(sid, f"aoa-resolved:{repo}#7@n3wsha0000") == ["app.py"]
    (item,) = store.outbox_since(sid)
    assert item["payload"]["status"] == "resolved" and not item["payload"]["retry"]
