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
