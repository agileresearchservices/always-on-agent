"""Scheduled eval runs with baseline comparison (flywheel follow-up, #61).

`EvalRunWorkflow` (started by a Temporal schedule, or by hand with `temporal schedule trigger aoa-eval-run`) runs the
behaviour suite against the real model in a subprocess (`eval_proposal.run_evals`: only the API key in its env), keeps
the per-case pass rates, compares them with the last *green* run and posts to Slack only for a regression nobody has
been told about yet. Real tokens: off unless `AOA_EVAL_SCHEDULE_HOURS` > 0, and a run is skipped when the prompt,
tools, safety rules and model are unchanged since the last run (unless that run is older than `MAX_AGE`).
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from .eval_compare import compare, report_text, signature, summarize

from .config import Settings
from .store import Store

KEEP_RUNS = 30
MAX_AGE = 7 * 86400  # run anyway when the last run is this old: the hosted model can change under a fixed name
FINGERPRINT_FILES = ("prompt.py", "tools.py", "safety.py", "events.py", "notify.py")
RunFn = Callable[[list[str]], Awaitable[dict[str, Any] | None]]


def fingerprint(settings: Settings) -> str:
    """Hash of what shapes the agent's behaviour: the files that define its prompt, tools and rules, and the models."""
    h = hashlib.sha256()
    here = Path(__file__).parent
    for f in FINGERPRINT_FILES:
        h.update(f.encode())
        h.update((here / f).read_bytes() if (here / f).exists() else b"")
    h.update(f"{settings.model}|{settings.helper_model}|{settings.review_model}".encode())
    return h.hexdigest()[:16]


async def run_scheduled(store: Store, settings: Settings, session: str, run: RunFn, *, now: float | None = None,
                        force: bool = False) -> dict[str, Any]:
    """One scheduled run. Returns {"status": "skipped"|"green"|"regression", ...}; posts to the outbox on a new regression."""
    now = time.time() if now is None else now
    fp = fingerprint(settings)
    runs = store.kv_get(session, "eval_runs", [])
    if not force and runs and runs[-1]["fingerprint"] == fp and now - runs[-1]["ts"] < MAX_AGE:
        return {"status": "skipped", "reason": "nothing that shapes the agent changed since the last run"}
    report = await run(["--runs", str(settings.eval_schedule_runs)])
    if report is None:
        return {"status": "skipped", "reason": "no ANTHROPIC_API_KEY for the eval run"}
    new = summarize(report, ts=now, fingerprint=fp)
    baseline = store.kv_get(session, "eval_baseline")
    regs = compare(baseline, new)
    store.kv_set(session, "eval_runs", (runs + [{**new, "regressions": regs}])[-KEEP_RUNS:])
    if not regs:
        store.kv_set(session, "eval_baseline", new)  # only a green run moves the baseline
        store.kv_set(session, "eval_last_posted", "")
        return {"status": "green", "est_cost_usd": new["est_cost_usd"]}
    sig = signature(regs)
    posted = store.kv_get(session, "eval_last_posted", "")
    if sig != posted:  # a failure that persists is reported once, not at every scheduled run
        store.outbox_push(session, "alert", {"text": report_text(regs, new)}, client_key=f"evalreg:{int(now)}:{sig}"[:200])
        store.kv_set(session, "eval_last_posted", sig)
    return {"status": "regression", "regressions": regs, "posted": sig != posted, "est_cost_usd": new["est_cost_usd"]}


def format_runs(runs: list[dict[str, Any]]) -> str:
    if not runs:
        return "no scheduled eval runs yet"
    out = []
    for r in runs:
        bad = r.get("regressions") or []
        cost = f" ${r['est_cost_usd']:.2f}" if r.get("est_cost_usd") else ""
        out.append(f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(r['ts']))}  {r['fingerprint']}  "
                   f"{'REGRESSION: ' + ', '.join(x['case'] for x in bad) if bad else 'green'}{cost}")
    return "\n".join(out)
