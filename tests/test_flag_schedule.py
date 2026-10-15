"""Scheduled flag scan and the schedule wiring (#64). No model, no Temporal server: the client is a fake."""
from __future__ import annotations

import pytest
from temporalio.client import ScheduleAlreadyRunningError
from temporalio.service import RPCError, RPCStatusCode

from aoa import flag_schedule as fs
from aoa import scheduling
from aoa.config import Settings
from aoa.store import Store


@pytest.fixture
def store(settings):
    return Store(settings.db_path)


def test_a_denied_approval_is_found_and_reported_once(settings, store):
    store.approval_request("ap1", "main", {"tool": "external_send", "to": "x"})
    store.approval_resolve("ap1", False)
    assert fs.run_flag_scan(store, settings, "main") == {"new": 1}
    assert fs.run_flag_scan(store, settings, "main") == {"new": 0}
    msgs = store.outbox_since("main", 0)
    assert len(msgs) == 1 and "approval_denied" in msgs[0]["payload"]["text"]


def test_digest_text_lists_kinds_and_says_nothing_was_proposed():
    added = [{"conv_id": "c1", "reason": "approval_denied:1", "evidence": "the person denied external_send"},
             {"conv_id": "c2", "reason": "cache_miss", "evidence": "missed"}]
    t = fs.digest_text(added)
    assert "2 new" in t and "approval_denied" in t and "cache_miss" in t and "Nothing was proposed" in t


def test_digest_caps_the_listing():
    added = [{"conv_id": f"c{i}", "reason": f"max_tokens:{i}", "evidence": "x"} for i in range(12)]
    t = fs.digest_text(added)
    assert t.count("• ") == fs.MAX_LISTED and "4 more" in t


def test_no_new_flags_posts_nothing(settings, store):
    assert fs.run_flag_scan(store, settings, "main") == {"new": 0}
    assert store.outbox_since("main", 0) == []


def test_new_flag_posts_one_digest_and_a_second_scan_posts_none(settings, store, monkeypatch):
    rows = [{"conv_id": "c1", "reason": "max_tokens:k", "step_key": "k", "evidence": "cut off"}]
    calls = iter([rows, []])
    monkeypatch.setattr(fs.flag, "scan", lambda st, se: next(calls))
    assert fs.run_flag_scan(store, settings, "main") == {"new": 1}
    assert fs.run_flag_scan(store, settings, "main") == {"new": 0}
    msgs = store.outbox_since("main", 0)
    assert [m["kind"] for m in msgs] == ["alert"] and "c1" in msgs[0]["payload"]["text"]


def test_a_retried_activity_cannot_post_the_same_digest_twice(settings, store, monkeypatch):
    rows = [{"conv_id": "c1", "reason": "max_tokens:k", "step_key": "k", "evidence": "cut off"}]
    monkeypatch.setattr(fs.flag, "scan", lambda st, se: rows)
    fs.run_flag_scan(store, settings, "main")
    fs.run_flag_scan(store, settings, "main")
    assert len(store.outbox_since("main", 0)) == 1


# --- schedule wiring ------------------------------------------------------------------------------------------------

class FakeHandle:
    def __init__(self, client):
        self.c = client

    async def delete(self):
        self.c.log.append("delete")
        if self.c.missing:
            raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")

    async def update(self, fn):
        self.c.log.append("update")


class FakeClient:
    def __init__(self, exists=False, missing=False):
        self.exists, self.missing, self.log, self.created = exists, missing, [], None

    def get_schedule_handle(self, sid):
        self.sid = sid
        return FakeHandle(self)

    async def create_schedule(self, sid, schedule):
        self.log.append("create")
        if self.exists:
            raise ScheduleAlreadyRunningError()
        self.created = (sid, schedule)


def with_settings(settings, **kw):
    import dataclasses
    return dataclasses.replace(settings, **kw)


async def test_flag_schedule_create_update_delete(settings):
    c = FakeClient()
    await scheduling.ensure_flag_schedule(c, with_settings(settings, flag_scan_hours=24))
    assert c.log == ["create"] and c.created[0] == "aoa-flag-scan" and c.created[1].action.workflow == "FlagScanWorkflow"
    c = FakeClient(exists=True)
    await scheduling.ensure_flag_schedule(c, with_settings(settings, flag_scan_hours=6))
    assert c.log == ["create", "update"]
    c = FakeClient()
    await scheduling.ensure_flag_schedule(c, with_settings(settings, flag_scan_hours=0))
    assert c.log == ["delete"]
    c = FakeClient(missing=True)  # off and already gone is fine
    await scheduling.ensure_flag_schedule(c, with_settings(settings, flag_scan_hours=0))


async def test_eval_schedule_needs_hours_and_a_key(settings, monkeypatch):
    on = with_settings(settings, eval_schedule_hours=12)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = FakeClient()
    await scheduling.ensure_eval_schedule(c, on)
    assert c.log == ["delete"]  # no key: no paid schedule
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    c = FakeClient()
    await scheduling.ensure_eval_schedule(c, on)
    assert c.log == ["create"] and c.created[1].action.workflow == "EvalRunWorkflow"
    c = FakeClient(exists=True)
    await scheduling.ensure_eval_schedule(c, on)
    assert c.log == ["create", "update"]
    c = FakeClient(missing=True)
    await scheduling.ensure_eval_schedule(c, with_settings(settings, eval_schedule_hours=0))
    assert c.log == ["delete"]


def test_flag_scan_setting_defaults_to_daily_and_zero_is_off(monkeypatch):
    monkeypatch.delenv("AOA_FLAG_SCAN_HOURS", raising=False)
    assert Settings.from_env().flag_scan_hours == 24
    monkeypatch.setenv("AOA_FLAG_SCAN_HOURS", "0")
    assert Settings.from_env().flag_scan_hours == 0


def test_evalruns_cli_prints_the_stored_runs(settings, capsys, monkeypatch):
    from aoa import __main__ as cli
    Store(settings.db_path).kv_set("main", "eval_runs", [
        {"ts": 1.0, "fingerprint": "abc", "est_cost_usd": 0.3, "regressions": []},
        {"ts": 2.0, "fingerprint": "def", "regressions": [{"case": "c1"}]}])
    monkeypatch.setattr("sys.argv", ["aoa", "evalruns"])
    cli.main()
    out = capsys.readouterr().out
    assert "abc" in out and "green" in out and "REGRESSION: c1" in out


async def test_activity_wrappers_run_the_scan_and_the_eval(make_core, monkeypatch):
    from aoa.activities import AgentActivities
    from aoa import eval_proposal
    core, _ = make_core([])
    acts = AgentActivities(core, None, core.s)
    core.store.approval_request("ap9", "main", {"tool": "external_send"})
    core.store.approval_resolve("ap9", False)
    assert await acts.flag_scan({"session": "main"}) == {"new": 1}

    async def fake(args, timeout=0):
        return {"model": "m", "runs_per_case": 3, "est_cost_usd": 0.1, "cases": {"a": {"hard": True, "passed": 3, "runs": 3}}}
    monkeypatch.setattr(eval_proposal, "run_evals", fake)
    out = await acts.run_scheduled_evals({"session": "main"})
    assert out["status"] == "green"
