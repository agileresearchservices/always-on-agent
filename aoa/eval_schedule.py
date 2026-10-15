"""Scheduled eval runs with baseline comparison (flywheel follow-up, #61).

`EvalRunWorkflow` (started by a Temporal schedule, or by hand with `temporal schedule trigger aoa-eval-run`) runs the
behaviour suite against the real model in a subprocess (`eval_proposal.run_evals`: only the API key in its env), keeps
the per-case pass rates, compares them with the last *green* run and posts to Slack only for a regression nobody has
been told about yet. Real tokens: off unless `AOA_EVAL_SCHEDULE_HOURS` > 0, and a run is skipped when the prompt,
tools, safety rules and model are unchanged since the last run (unless that run is older than `MAX_AGE`).
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from .eval_compare import compare, report_text, signature, summarize

from .config import Settings
from .store import Store

KEEP_RUNS = 30
GREEN_WINDOW = 5  # soft cases are held to their best rate over this many recent green runs
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
    for f in sorted((here.parent / "evals").rglob("*.py")) + sorted((here.parent / "evals" / "accepted").glob("*.json")):
        h.update(f.name.encode())
        h.update(f.read_bytes())
    h.update(f"runs={settings.eval_schedule_runs}|".encode())
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
    try:
        report = await run(["--runs", str(settings.eval_schedule_runs)])
        cases = (report or {}).get("cases", {})
        if cases and not any(c["passed"] for c in cases.values()):
            # the harness grades a crashed run as a failed case, so a bad key or an outage looks like "everything
            # regressed". A real behaviour regression does not fail every soft case too: call it an error.
            why = next((f for c in cases.values() for f in c.get("failures", []) if "raised" in f), "")
            raise RuntimeError(f"every case failed in all runs, so the eval harness or the API is probably broken {why}".strip())
    except Exception as e:  # a crash or timeout: the activity is never retried, so say so once instead of staying silent
        msg = _scrub(f"{type(e).__name__}: {e}")
        sig = msg[:60]
        first = sig != store.kv_get(session, "eval_last_error", "")
        if first:
            store.outbox_push(session, "alert", {"text": f":warning: *Scheduled eval run failed* (nothing was compared): {msg[:300]}\n"
                                                         "Nothing changes until the next run; check `data/logs/worker.log`."},
                              client_key=f"evalerr:{int(now)}:{sig}"[:200])
            store.kv_set(session, "eval_last_error", sig)
        return {"status": "error", "error": msg, "posted": first}
    if report is None:
        return {"status": "skipped", "reason": "no ANTHROPIC_API_KEY for the eval run"}
    new = summarize(report, ts=now, fingerprint=fp)
    green = [r for r in runs if not r.get("regressions")][-GREEN_WINDOW:]
    regs = compare(green or store.kv_get(session, "eval_baseline"), new)
    store.kv_set(session, "eval_runs", (runs + [{**new, "regressions": regs}])[-KEEP_RUNS:])
    was_bad = bool(store.kv_get(session, "eval_last_posted", "") or store.kv_get(session, "eval_last_error", ""))
    store.kv_set(session, "eval_last_error", "")
    if not regs:
        store.kv_set(session, "eval_baseline", new)  # only a green run moves the baseline
        store.kv_set(session, "eval_last_posted", "")
        if was_bad:  # close the loop on an alert the person was shown
            store.outbox_push(session, "update", {"text": ":white_check_mark: Scheduled evals are green again."},
                              client_key=f"evalok:{int(now)}")
        return {"status": "green", "est_cost_usd": new["est_cost_usd"]}
    sig = signature(regs)
    posted = store.kv_get(session, "eval_last_posted", "")
    if sig != posted:  # a failure that persists is reported once, not at every scheduled run
        store.outbox_push(session, "alert", {"text": report_text(regs, new)}, client_key=f"evalreg:{int(now)}:{sig}"[:200])
        store.kv_set(session, "eval_last_posted", sig)
    return {"status": "regression", "regressions": regs, "posted": sig != posted, "est_cost_usd": new["est_cost_usd"]}


def _scrub(text: str) -> str:
    """Error text goes to Slack: drop anything shaped like an API key, and the configured one verbatim."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        text = text.replace(key, "[key]")
    return re.sub(r"sk-ant-[A-Za-z0-9_\-]+", "[key]", text)


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
