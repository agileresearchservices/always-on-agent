"""Scheduled eval runs and baseline comparison (#61). No model, no server: the eval subprocess is a stub."""
from __future__ import annotations

import pytest

from aoa import eval_schedule as es
from aoa.store import Store
from aoa.eval_compare import compare, signature, summarize


def report(**cases):
    """cases: name=(hard, passed, runs)"""
    return {"model": "m", "runs_per_case": 3, "est_cost_usd": 0.3,
            "cases": {n: {"hard": h, "passed": p, "runs": r} for n, (h, p, r) in cases.items()}}


def summ(**cases):
    return summarize(report(**cases), ts=1.0, fingerprint="f")


def test_green_run_has_no_regressions():
    assert compare(summ(a=(True, 3, 3), b=(False, 3, 3)), summ(a=(True, 3, 3), b=(False, 3, 3))) == []


def test_hard_failure_is_a_regression_even_without_a_baseline():
    regs = compare(None, summ(a=(True, 2, 3)))
    assert [(r["case"], r["kind"]) for r in regs] == [("a", "hard")]


def test_soft_drop_needs_more_than_one_lost_run():
    base = summ(s=(False, 3, 3))
    assert compare(base, summ(s=(False, 2, 3))) == []  # one run lost is noise
    assert [r["case"] for r in compare(base, summ(s=(False, 1, 3)))] == ["s"]


def test_soft_case_already_low_in_the_baseline_is_not_a_regression():
    assert compare(summ(s=(False, 1, 3)), summ(s=(False, 1, 3))) == []


@pytest.fixture
def env(settings):
    return Store(settings.db_path), settings


def stub(*reports):
    it = iter(reports)
    calls = []

    async def run(args):
        calls.append(args)
        return next(it)
    run.calls = calls
    return run


GREEN = report(a=(True, 3, 3), b=(False, 3, 3))
BROKEN = report(a=(True, 0, 3), b=(False, 3, 3))


def alerts(store):
    return [m for m in store.outbox_since("main", 0) if m["kind"] == "alert"]


async def test_first_green_run_sets_the_baseline_and_posts_nothing(env):
    store, s = env
    out = await es.run_scheduled(store, s, "main", stub(GREEN), now=100.0)
    assert out["status"] == "green" and store.kv_get("main", "eval_baseline")["cases"]["a"]["passed"] == 3
    assert alerts(store) == []


async def test_a_regression_is_posted_exactly_once_and_keeps_the_baseline(env):
    store, s = env
    await es.run_scheduled(store, s, "main", stub(GREEN), now=100.0)
    out = await es.run_scheduled(store, s, "main", stub(BROKEN, BROKEN), now=100.0 + es.MAX_AGE + 1)
    assert out["status"] == "regression" and out["posted"] and len(alerts(store)) == 1
    out2 = await es.run_scheduled(store, s, "main", stub(BROKEN), now=100.0 + 2 * es.MAX_AGE + 2)
    assert out2["status"] == "regression" and not out2["posted"] and len(alerts(store)) == 1  # same failure, no new post
    assert store.kv_get("main", "eval_baseline")["cases"]["a"]["passed"] == 3  # still the last green run
    assert "`a`" in alerts(store)[0]["payload"]["text"]


async def test_recovery_clears_the_dedupe_so_a_later_regression_posts_again(env):
    store, s = env
    t = 100.0
    for rep in (BROKEN, GREEN, BROKEN):
        t += es.MAX_AGE + 1
        await es.run_scheduled(store, s, "main", stub(rep), now=t)
    assert len(alerts(store)) == 2


async def test_unchanged_agent_skips_the_paid_run(env):
    store, s = env
    run = stub(GREEN)
    await es.run_scheduled(store, s, "main", run, now=100.0)
    out = await es.run_scheduled(store, s, "main", run, now=200.0)
    assert out["status"] == "skipped" and len(run.calls) == 1
    forced = await es.run_scheduled(store, s, "main", stub(GREEN), now=300.0, force=True)
    assert forced["status"] == "green"


async def test_no_api_key_is_a_skip_not_a_crash(env):
    store, s = env

    async def none(args):
        return None
    assert (await es.run_scheduled(store, s, "main", none, now=1.0))["status"] == "skipped"
    assert store.kv_get("main", "eval_runs", []) == []


def test_signature_is_stable():
    assert signature(compare(None, summ(a=(True, 0, 3), b=(True, 1, 3)))) == "hard:a,hard:b"


def test_zero_runs_and_missing_cases_are_regressions():
    assert [r["case"] for r in compare(None, summ(a=(True, 0, 0)))] == ["a"]
    assert [r["case"] for r in compare(summ(a=(True, 3, 3), b=(False, 3, 3)), summ(a=(True, 3, 3)))] == ["b"]


# --- #64: failure alert, recovery note, best-of-window soft guard -----------------------------------------------------

def boom(exc):
    async def run(args):
        raise exc
    return run


def texts(store, kind):
    return [m["payload"]["text"] for m in store.outbox_since("main", 0) if m["kind"] == kind]


async def test_a_crashed_run_alerts_once_and_a_green_run_closes_it(env):
    store, s = env
    out = await es.run_scheduled(store, s, "main", boom(RuntimeError("eval run failed: 401")), now=100.0)
    assert out["status"] == "error" and out["posted"] and len(alerts(store)) == 1
    soon = await es.run_scheduled(store, s, "main", boom(RuntimeError("eval run failed: 401")), now=200.0)
    assert soon["status"] == "skipped"  # backoff: no paid re-run of the same setup within a day
    again = await es.run_scheduled(store, s, "main", boom(RuntimeError("eval run failed: 401")), now=200.0 + es.ERROR_BACKOFF)
    assert again["status"] == "error" and not again["posted"] and len(alerts(store)) == 1  # same failure, no new post
    assert store.kv_get("main", "eval_runs", []) == []  # a failed run is not a result
    ok = await es.run_scheduled(store, s, "main", stub(GREEN), now=300.0 + 2 * es.ERROR_BACKOFF)
    assert ok["status"] == "green" and len(texts(store, "update")) == 1 and "green again" in texts(store, "update")[0]


async def test_a_timeout_with_no_message_still_alerts(env):
    store, s = env
    out = await es.run_scheduled(store, s, "main", boom(TimeoutError()), now=1.0)
    assert out["status"] == "error" and "TimeoutError" in alerts(store)[0]["payload"]["text"]


async def test_error_text_never_carries_a_key(env, monkeypatch):
    store, s = env
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secretvalue123")
    await es.run_scheduled(store, s, "main", boom(RuntimeError("bad key sk-ant-secretvalue123 and sk-ant-other_456")), now=1.0)
    text = alerts(store)[0]["payload"]["text"]
    assert "secretvalue" not in text and "other_456" not in text


async def test_a_green_run_with_nothing_to_close_stays_silent(env):
    store, s = env
    await es.run_scheduled(store, s, "main", stub(GREEN), now=100.0)
    await es.run_scheduled(store, s, "main", stub(GREEN), now=100.0 + es.MAX_AGE + 1)
    assert texts(store, "update") == [] and alerts(store) == []


def test_soft_case_is_held_to_the_best_of_the_window():
    window = [summ(s=(False, 3, 3)), summ(s=(False, 2, 3)), summ(s=(False, 2, 3))]
    assert compare(window[-1], summ(s=(False, 1, 3))) == []  # against only the last run: -0.33, the old blind spot
    regs = compare(window, summ(s=(False, 1, 3)))
    assert [r["case"] for r in regs] == ["s"] and "best" in regs[0]["detail"]


async def test_slow_soft_slide_across_scheduled_runs_alerts(env):
    store, s = env
    t = 100.0
    for p in (3, 2, 2, 1):  # each step is within the noise band of the previous run
        t += es.MAX_AGE + 1
        await es.run_scheduled(store, s, "main", stub(report(s=(False, p, 3))), now=t)
    assert len(alerts(store)) == 1 and "`s`" in alerts(store)[0]["payload"]["text"]


async def test_a_run_where_nothing_passed_is_an_error_not_a_regression(env):
    store, s = env
    dead = report(a=(True, 0, 3), b=(False, 0, 3))
    dead["cases"]["a"]["failures"] = ["run 1 raised AuthenticationError"]
    out = await es.run_scheduled(store, s, "main", stub(dead), now=1.0)
    assert out["status"] == "error" and "probably broken" in out["error"]
    assert store.kv_get("main", "eval_runs", []) == [] and "Eval regression" not in alerts(store)[0]["payload"]["text"]
    assert "failed" in alerts(store)[0]["payload"]["text"]


async def test_a_total_failure_without_a_crash_is_still_a_regression(env):
    store, s = env
    out = await es.run_scheduled(store, s, "main", stub(report(a=(True, 0, 3), b=(False, 0, 3))), now=1.0)
    assert out["status"] == "regression"
