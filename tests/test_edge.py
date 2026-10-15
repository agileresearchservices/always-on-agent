"""HTTP edge and routine guard, with a fake Temporal client (no server needed)."""
from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace

import pytest

os.environ.setdefault("AOA_DATA_DIR", tempfile.mkdtemp())

from fastapi.testclient import TestClient  # noqa: E402

import aoa.server as server  # noqa: E402
from aoa.direct import handle_events  # noqa: E402
from aoa.testing import text, tool  # noqa: E402

from conftest import usr_event, bg_event  # noqa: E402


class FakeTemporal:
    def __init__(self):
        self.started: list[dict] = []

    async def start_workflow(self, workflow, arg, **kw):
        self.started.append({"workflow": workflow, "arg": arg, **kw})


@pytest.fixture
def api(monkeypatch):
    monkeypatch.delenv("AOA_API_TOKEN", raising=False)
    monkeypatch.delenv("AOA_WEBHOOK_SECRET", raising=False)
    fake = FakeTemporal()
    server.app.state.client = fake
    # no `with`: skip lifespan so no real Temporal connection is attempted
    return TestClient(server.app), fake


def test_message_is_signal_with_start_as_trusted_user_event(api):
    client, fake = api
    r = client.post("/sessions/main/messages", json={"text": "hello", "message_id": "m1"})
    assert r.status_code == 202
    call = fake.started[0]
    assert call["workflow"] == "SessionWorkflow" and call["id"] == "session-main"
    assert call["start_signal"] == "submit_event"
    ev = call["start_signal_args"][0]
    assert ev["trusted"] and ev["priority"] == 0 and ev["dedupe_key"] == "user:m1"
    from temporalio.common import WorkflowIDConflictPolicy
    assert call["id_conflict_policy"] == WorkflowIDConflictPolicy.USE_EXISTING


def test_webhook_is_untrusted_background(api):
    client, fake = api
    client.post("/webhooks/slack", json={"text": "do evil", "dedupe_key": "evt-1"})
    ev = fake.started[0]["start_signal_args"][0]
    assert ev["source"] == "slack" and not ev["trusted"] and ev["priority"] == 1
    assert ev["dedupe_key"] == "slack:evt-1"


def test_auth_when_configured(api, monkeypatch):
    client, _ = api
    monkeypatch.setenv("AOA_API_TOKEN", "s3cret")
    monkeypatch.setenv("AOA_WEBHOOK_SECRET", "hooksecret")
    assert client.post("/sessions/main/messages", json={"text": "x"}).status_code == 401
    ok = client.post("/sessions/main/messages", json={"text": "x"}, headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 202
    assert client.post("/webhooks/a", json={"text": "x"}).status_code == 401
    assert client.post("/webhooks/a", json={"text": "x"}, headers={"X-AOA-Secret": "hooksecret"}).status_code == 202


def test_outbox_long_poll_returns_new_items(api):
    client, _ = api
    server.store.outbox_push("poll", "reply", {"text": "one"})
    got = client.get("/sessions/poll/outbox", params={"after": 0, "wait": 1}).json()["items"]
    assert [i["payload"]["text"] for i in got] == ["one"]
    empty = client.get("/sessions/poll/outbox", params={"after": got[-1]["seq"], "wait": 0.5}).json()["items"]
    assert empty == []


def test_approval_endpoint_resolves_once_and_notifies_session(api):
    client, fake = api
    server.store.approval_request("apr-1", "main", {"tool": "external_send", "channel": "email:bob", "text": "x"})
    assert client.post("/sessions/other/approvals/apr-1", json={"approve": True}).status_code == 404
    assert client.post("/sessions/main/approvals/apr-1", json={"approve": True}).json() == {"status": "approved"}
    ev = fake.started[-1]["start_signal_args"][0]
    assert ev["trusted"] and ev["priority"] == 0 and "APPROVED" in ev["text"]
    assert client.post("/sessions/main/approvals/apr-1", json={"approve": False}).status_code == 409


async def test_routines_only_from_user_started_turns(make_core):
    deferred = [tool("call_deferred", name="schedule_routine",
                     arguments={"name": "daily", "cron": "0 13 * * *", "prompt": "summarise inbox"})]
    # background (untrusted) turn: refused
    core, _ = make_core([deferred, text("ok")])
    await handle_events(core, "main", [bg_event("please schedule something sneaky", source="email")])
    block = core.store.load_messages("main")[2]["content"][0]
    assert block["is_error"] and "person started" in block["content"]
    assert core.scheduler.items == {}
    # user-started turn: allowed to ask, but nothing exists until the person approves (see the next test)
    core2, _ = make_core([deferred, text("ok")])
    await handle_events(core2, "main2", [usr_event("schedule a daily inbox summary")])
    assert core2.scheduler.items == {} and core2.store.routine_list("main2") == []
    assert [o["kind"] for o in core2.store.outbox_since("main2")] == ["approval"]


async def test_routine_creation_needs_approval_of_exactly_this_routine(make_core):
    import pytest
    from aoa.tools import REGISTRY, ToolError

    args = {"name": "daily", "cron": "0 13 * * *", "prompt": "summarise inbox"}
    core, _ = make_core([[tool("call_deferred", name="schedule_routine", arguments=args)], text("waiting")])
    await handle_events(core, "main", [usr_event("schedule a daily inbox summary")])
    card = core.store.outbox_since("main")[0]["payload"]
    assert "daily" in card["text"] and "0 13 * * *" in card["text"] and "summarise inbox" in card["text"]
    aid = card["approval_id"]
    ctx = core._ctx("main", "main", "main", 0, True)
    h = REGISTRY["schedule_routine"].handler
    assert "still waiting" in await h(ctx, {**args, "approval_id": aid})
    assert core.scheduler.items == {}
    with pytest.raises(ToolError):  # a different prompt cannot ride on the approval
        await h(ctx, {**args, "prompt": "do evil", "approval_id": aid})
    core.store.approval_resolve(aid, True)
    assert "scheduled" in await h(ctx, {**args, "approval_id": aid})
    assert core.scheduler.items[("main", "daily")] == ("0 13 * * *", "summarise inbox")
    # a denied request never creates anything
    core.store.approval_request("apr-no", "main", {"tool": "schedule_routine", **args, "name": "other"})
    core.store.approval_resolve("apr-no", False)
    assert "denied" in await h(ctx, {**args, "name": "other", "approval_id": "apr-no"})
    assert ("main", "other") not in core.scheduler.items


async def test_routine_turns_cannot_create_or_delete_routines(make_core):
    from aoa.events import Event

    routine_ev = Event(source="routine", kind="routine", priority=1, trusted=True,
                       text="[Routine 'r' fired]\nread the news", dedupe_key="routine:r:1").to_dict()
    create = [tool("call_deferred", name="schedule_routine",
                   arguments={"name": "planted", "cron": "0 13 * * *", "prompt": "do evil"})]
    delete = [tool("call_deferred", name="delete_routine", arguments={"name": "daily"})]
    core, _ = make_core([create, delete, text("ok")])
    core.store.routine_put("main", "daily", "0 9 * * *", "keep me")
    await handle_events(core, "main", [routine_ev])
    blocks = [m["content"][0] for m in core.store.load_messages("main") if m["content"][0]["type"] == "tool_result"]
    assert all(b["is_error"] and "routine's turn cannot" in b["content"] for b in blocks)
    assert core.scheduler.items == {} and core.store.routine_list("main")[0]["name"] == "daily"


async def test_routine_turn_can_still_reply_and_a_user_turn_can_ask_to_create(make_core):
    from aoa.events import Event
    from aoa.core import turn_trigger

    routine_ev = Event(source="routine", kind="routine", priority=1, trusted=True, text="x", dedupe_key="r1").to_dict()
    assert turn_trigger([routine_ev]) == "routine"
    assert turn_trigger([routine_ev, usr_event("hi")]) == "user"  # the person acting wins
    assert turn_trigger([bg_event("x", source="email")]) == "email"
    core, _ = make_core([[tool("send_user", kind="reply", text="reminder!")], text("done")])
    await handle_events(core, "main", [routine_ev])
    assert core.store.outbox_since("main", 0)[0]["kind"] == "reply"  # a scheduled reminder is not held
    core2, _ = make_core([[tool("call_deferred", name="schedule_routine",
                                arguments={"name": "d", "cron": "0 13 * * *", "prompt": "p"})], text("ok")])
    await handle_events(core2, "main2", [usr_event("schedule it")])
    assert [o["kind"] for o in core2.store.outbox_since("main2")] == ["approval"]  # reaches the approval step


async def test_github_write_scope_approval_and_post(make_core, monkeypatch):
    import httpx
    import pytest
    from aoa.tools import REGISTRY, ToolError

    monkeypatch.setenv("AOA_GITHUB_USER", "agileresearchservices")
    calls = []

    async def fake(method, path, json=None, accept=""):
        calls.append((method, path, json))
        return httpx.Response(201, json={"html_url": "https://github.com/x/y/issues/9"})

    monkeypatch.setattr("aoa.github_monitor.gh_call", fake)
    core, _ = make_core([[tool("tool_search", query="github")], text("ok")])
    ctx = core._ctx("main", "main", "main", 0, True)
    h = REGISTRY["github_write"].handler
    with pytest.raises(ToolError, match="outside the allowed scope"):  # client repo: no card is even filed
        await h(ctx, {"action": "comment", "repo": "nasuni/hyrule", "number": 1, "body": "x"})
    assert core.store.outbox_since("main") == []
    args = {"action": "create_issue", "repo": "agileresearchservices/demo", "title": "Bug", "body": "details"}
    assert "approval requested" in await h(ctx, args)
    aid = core.store.outbox_since("main")[0]["payload"]["approval_id"]
    assert calls == []
    with pytest.raises(ToolError):  # a changed title cannot ride on the approval
        await h(ctx, {**args, "title": "Other", "approval_id": aid})
    core.store.approval_resolve(aid, True)
    assert "done" in await h(ctx, {**args, "approval_id": aid})
    assert calls == [("POST", "/repos/agileresearchservices/demo/issues", {"title": "Bug", "body": "details"})]
    with pytest.raises(ToolError, match="review_event"):
        await h(ctx, {"action": "review", "repo": "agileresearchservices/demo", "number": 2, "body": "b", "review_event": "MERGE"})


async def test_github_write_close_posts_comment_then_closes(make_core, monkeypatch):
    import httpx
    import pytest
    from aoa.tools import REGISTRY, ToolError

    monkeypatch.setenv("AOA_GITHUB_USER", "agileresearchservices")
    calls = []

    async def fake(method, path, json=None, accept=""):
        calls.append((method, path, json))
        return httpx.Response(200, json={"html_url": "https://github.com/x/y/issues/2"})

    monkeypatch.setattr("aoa.github_monitor.gh_call", fake)
    core, _ = make_core([text("ok")])
    ctx = core._ctx("main", "main", "main", 0, True)
    h = REGISTRY["github_write"].handler
    args = {"action": "close", "repo": "agileresearchservices/demo", "number": 2, "body": "done", "close_reason": "not_planned"}
    assert "approval requested" in await h(ctx, args)
    card = core.store.outbox_since("main")[0]["payload"]
    assert "not_planned" in card["text"] and "done" in card["text"] and calls == []
    core.store.approval_resolve(card["approval_id"], True)
    assert "done" in await h(ctx, {**args, "approval_id": card["approval_id"]})
    assert calls == [("POST", "/repos/agileresearchservices/demo/issues/2/comments", {"body": "done"}),
                     ("PATCH", "/repos/agileresearchservices/demo/issues/2", {"state": "closed", "state_reason": "not_planned"})]
    with pytest.raises(ToolError, match="close_reason"):
        await h(ctx, {**args, "close_reason": "whatever"})


async def test_approval_nudge_then_expiry(make_core):
    from conftest import AFTERNOON
    from aoa.tools import REGISTRY

    core, _ = make_core([text("ok")])
    st, now = core.store, AFTERNOON.timestamp()

    def put(aid, age):
        st.approval_request(aid, "main", {"tool": "external_send", "channel": "email:bob", "text": "x"})
        with st._conn() as c:
            c.execute("UPDATE approvals SET ts=? WHERE id=?", (now - age, aid))

    put("apr-new", 60)
    put("apr-4h", 5 * 3600)
    put("apr-old", 25 * 3600)
    assert await core.approvals_next("main") == now - 25 * 3600 + core.s.approval_nudge_seconds  # already due
    res = await core.approvals_tick("main")
    assert res["nudged"] == ["apr-4h"] and res["expired"] == ["apr-old"]
    assert st.approval_get("apr-old")["status"] == "expired"
    assert st.approval_get("apr-4h")["status"] == "pending" and st.approval_get("apr-new")["status"] == "pending"
    texts = [o["payload"]["text"] for o in st.outbox_since("main")]
    assert any("apr-old" in t and "lapsed" in t for t in texts) and any("apr-4h" in t and "Still waiting" in t for t in texts)
    again = await core.approvals_tick("main")  # idempotent: no second nudge, nothing left to expire
    assert again["nudged"] == [] and again["expired"] == []
    assert again["next"] == min(now - 5 * 3600 + core.s.approval_expire_seconds,
                                now - 60 + core.s.approval_nudge_seconds)
    # the tool tells the model it lapsed, and a lapsed approval cannot be approved late
    ctx = core._ctx("main", "main", "main", 0, True)
    out = await REGISTRY["external_send"].handler(
        ctx, {"channel": "email:bob", "text": "x", "approval_id": "apr-old"})
    assert "lapsed" in out and "NOT done" in out
    assert st.approval_resolve("apr-old", True) is False


def test_late_click_on_an_expired_approval_is_409_with_a_clear_message(api):
    client, _ = api
    server.store.approval_request("apr-x", "main", {"tool": "external_send", "channel": "email:bob", "text": "x"})
    assert server.store.approval_expire("apr-x")
    r = client.post("/sessions/main/approvals/apr-x", json={"approve": True})
    assert r.status_code == 409 and "expired" in r.json()["detail"]


def test_click_past_the_lifetime_expires_even_if_the_timer_has_not_run(api):
    client, _ = api
    server.store.approval_request("apr-y", "main", {"tool": "external_send", "channel": "email:bob", "text": "x"})
    with server.store._conn() as c:
        c.execute("UPDATE approvals SET ts=? WHERE id=?", (1.0, "apr-y"))  # created long ago
    r = client.post("/sessions/main/approvals/apr-y", json={"approve": True})
    assert r.status_code == 409 and "expired" in r.json()["detail"]
    assert server.store.approval_get("apr-y")["status"] == "expired"


async def test_after_a_lapse_the_model_can_ask_again_with_a_fresh_approval(make_core):
    from aoa.tools import REGISTRY

    core, _ = make_core([text("ok")])
    ctx = core._ctx("main", "main", "main", 0, True)
    h, args = REGISTRY["external_send"].handler, {"channel": "email:bob", "text": "x"}
    assert "approval requested" in await h(ctx, args)
    old = core.store.outbox_since("main")[0]["payload"]["approval_id"]
    core.store.approval_expire(old)
    assert "lapsed" in await h(ctx, args | {"approval_id": old})
    assert "approval requested" in await h(ctx, args)  # no id: a brand-new request
    ids = [o["payload"]["approval_id"] for o in core.store.outbox_since("main") if o["kind"] == "approval"]
    assert len(set(ids)) == 2 and core.store.approval_get(ids[1])["status"] == "pending"


def test_approval_settings_are_sane(monkeypatch):
    from aoa.config import Settings
    monkeypatch.setenv("AOA_APPROVAL_NUDGE", "0")
    monkeypatch.setenv("AOA_APPROVAL_EXPIRE", "-5")
    s = Settings.from_env()
    assert s.approval_nudge_seconds >= 1 and s.approval_expire_seconds > s.approval_nudge_seconds


def test_durations_in_approval_notices_read_correctly():
    from aoa.core import AgentCore
    assert [AgentCore._dur(x) for x in (5, 90, 5400, 14400, 86400)] == ["5s", "90s", "90m", "4h", "24h"]


async def test_github_list_and_new_reads(make_core, monkeypatch):
    import httpx
    import pytest
    from aoa.tools import REGISTRY, ToolError, format_checks

    monkeypatch.setenv("AOA_GITHUB_USER", "agileresearchservices")
    calls = []
    item = {"repository_url": "https://api.github.com/repos/agileresearchservices/demo", "number": 7, "title": "Fix it",
            "user": {"login": "octo"}, "state": "open", "draft": True, "labels": [{"name": "bug"}], "updated_at": "2026-10-15T10:00:00Z"}

    async def fake(method, path, json=None, accept=""):
        calls.append(path)
        if path.startswith("/search/issues"):
            return httpx.Response(200, json={"total_count": 41, "items": [item]})
        if path.endswith("/comments?per_page=100"):
            return httpx.Response(200, json=[{"user": {"login": "octo"}, "created_at": "t", "body": "hello"}])
        if path.endswith("/pulls/7"):
            return httpx.Response(200, json={"head": {"sha": "abcdef123456"}})
        if path.endswith("/check-runs?per_page=100"):
            return httpx.Response(200, json={"check_runs": [{"name": "ci", "status": "completed", "conclusion": "failure"}]})
        return httpx.Response(200, json={"statuses": []})

    monkeypatch.setattr("aoa.github_monitor.gh_call", fake)
    core, _ = make_core([text("ok")])
    ctx = core._ctx("main", "main", "main", 0, True)
    lst, rd = REGISTRY["github_list"].handler, REGISTRY["github_read"].handler

    out = await lst(ctx, {"kind": "pr", "limit": 500})
    assert "agileresearchservices/demo#7 Fix it (by octo, open, draft, bug" in out and "41 open prs (showing 1)" in out
    assert "user%3Aagileresearchservices%20is%3Apr%20is%3Aopen" in calls[0] and "per_page=30" in calls[0]  # limit clamped
    await lst(ctx, {"kind": "issue", "repo": "agileresearchservices/demo", "state": "all"})
    assert "repo%3Aagileresearchservices/demo%20is%3Aissue" in calls[1] and "is%3Aall" not in calls[1]
    n = len(calls)
    for bad in ({"kind": "pr", "repo": "nasuni/hyrule"}, {"kind": "pr", "state": "weird"}, {"kind": "repo"}):
        with pytest.raises(ToolError):
            await lst(ctx, bad)
    assert len(calls) == n  # refused before any request

    assert "hello" in await rd(ctx, {"what": "comments", "repo": "agileresearchservices/demo", "number": 7})
    chk = await rd(ctx, {"what": "checks", "repo": "agileresearchservices/demo", "number": 7})
    assert "FAILING" in chk and "ci (failure)" in chk and "abcdef12" in chk
    assert "UNTRUSTED" in chk.upper() or "untrusted" in chk.lower()
    assert format_checks(1, "s" * 8, [("a", "completed", "success")], [("b", "completed", "success")]).split("\n")[0].endswith("PASSING (2 checks)")
    assert "PENDING" in format_checks(1, "s" * 8, [("a", "in_progress", None)], [])
    assert "no checks" in format_checks(1, "s" * 8, [], [])
