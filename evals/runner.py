"""Run cases against the real model in throwaway state, with every external effect faked.

Faked: the scheduler (in memory), external_send (already a stub), GitHub (gh_call is replaced by a
recorder that serves a canned PR for reads and records writes). Real: the model, the prompt, the tools,
the notifier, the store.
"""
from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from aoa import github_monitor
from aoa.config import Settings
from aoa.core import AgentCore
from aoa.direct import handle_events
from aoa.llm import LLM
from aoa.memory import MemoryStore
from aoa.store import Store
from aoa.testing import MemoryScheduler

from .cases import CASES, Case
from .graders import Run, ToolCall

SESSION = "main"
_current: contextvars.ContextVar[tuple[Run, Case] | None] = contextvars.ContextVar("eval_run", default=None)


async def _fake_gh_call(method: str, path: str, json: Any = None, accept: str = "") -> httpx.Response:
    cur = _current.get()
    if cur is None:
        raise RuntimeError("gh_call outside an eval run")
    run, case = cur
    run.gh_calls.append((method, path, json))
    fx = case.pr_fixture
    if method == "GET" and fx and "/pulls/" in path:
        if "diff" in accept:
            return httpx.Response(200, text=fx["diff"])
        return httpx.Response(200, json={"title": fx["title"], "body": fx["body"], "state": "open", "user": {"login": "octo"}})
    return httpx.Response(200, json={"html_url": "https://github.example/fake"})


def install_fakes() -> None:
    github_monitor.gh_call = _fake_gh_call  # type: ignore[assignment]
    os.environ["AOA_GITHUB_USER"] = "agileresearchservices"


def _collect(core: AgentCore, run: Run) -> None:
    run.outbox = [{"kind": o["kind"], "payload": o["payload"]} for o in core.store.outbox_since(SESSION)]
    run.routines = core.store.routine_list(SESSION)
    run.scheduled = dict(core.scheduler.items)
    run.digest = core.store.kv_get(SESSION, "digest", [])
    u = core.store.usage_totals(SESSION)
    run.output_tokens, run.input_tokens, run.cache_read, run.cache_write = u["output"], u["input"], u["cache_read"], u["cache_write"]
    for m in core.store.load_messages(SESSION):
        if m["role"] != "assistant":
            continue
        run.steps += 1
        for b in m["content"]:
            if b.get("type") == "tool_use":
                name, args = b["name"], b["input"]
                if name == "call_deferred":
                    name, args = args.get("name", "?"), args.get("arguments", {})
                run.tool_calls.append(ToolCall(name, args))


async def run_case_once(case: Case, llm: LLM, base: Settings) -> Run:
    run = Run()
    token = _current.set((run, case))
    try:
        with tempfile.TemporaryDirectory(prefix="aoa-eval-") as d:
            s = dataclasses.replace(base, data_dir=Path(d))
            core = AgentCore(s, Store(s.db_path), MemoryStore(s.memory_dir), llm, MemoryScheduler(),
                             clock=lambda: case.clock)
            try:
                for events in case.turns:
                    await handle_events(core, SESSION, events)
            except Exception as e:  # noqa: BLE001 - a crashed run is a failed run, with the reason kept
                run.error = f"{type(e).__name__}: {e}"
            _collect(core, run)
    finally:
        _current.reset(token)
    return run


def grade(case: Case, run: Run) -> list[str]:
    """Names + reasons of failed graders (empty = pass)."""
    failures = []
    for g in case.graders:
        ok, msg = g(run)
        if not ok:
            failures.append(msg)
    return failures


async def run_suite(
    cases: list[Case], runs: int, llm_factory: Callable[[], LLM], concurrency: int = 4, settings: Settings | None = None,
) -> dict[str, Any]:
    install_fakes()
    base = settings or Settings.from_env()
    sem = asyncio.Semaphore(concurrency)

    async def one(case: Case, i: int) -> dict[str, Any]:
        async with sem:
            run = await run_case_once(case, llm_factory(), base)
        failures = grade(case, run)
        return {"case": case.name, "run": i, "passed": not failures, "failures": failures,
                "steps": run.steps, "output_tokens": run.output_tokens, "input_tokens": run.input_tokens,
                "cache_read": run.cache_read, "cache_write": run.cache_write, "kinds": [o["kind"] for o in run.outbox],
                "tools": [c.name for c in run.tool_calls]}

    t0 = time.time()
    results = await asyncio.gather(*(one(c, i) for c in cases for i in range(runs)))
    by_case: dict[str, dict[str, Any]] = {}
    for c in cases:
        rs = [r for r in results if r["case"] == c.name]
        by_case[c.name] = {"hard": c.hard, "passed": sum(r["passed"] for r in rs), "runs": len(rs),
                           "failures": sorted({f for r in rs for f in r["failures"]})}
    tot = {k: sum(r[k] for r in results) for k in ("input_tokens", "output_tokens", "cache_read", "cache_write")}
    return {"model": base.model, "runs_per_case": runs, "seconds": round(time.time() - t0, 1), "tokens": tot,
            "cases": by_case, "results": results}


def format_table(report: dict[str, Any]) -> str:
    t = report["tokens"]
    lines = [f"model {report['model']}  runs/case {report['runs_per_case']}  {report['seconds']}s", 
             f"tokens: {t['input_tokens']} in, {t['output_tokens']} out, {t['cache_read']} cache read, {t['cache_write']} cache write", ""]
    for name, c in report["cases"].items():
        mark = "ok  " if c["passed"] == c["runs"] else ("FAIL" if c["hard"] else "warn")
        lines.append(f"{mark} {'hard' if c['hard'] else 'soft'} {c['passed']}/{c['runs']}  {name}")
        for f in c["failures"]:
            lines.append(f"       - {f[:160]}")
    hard_bad = [n for n, c in report["cases"].items() if c["hard"] and c["passed"] != c["runs"]]
    lines += ["", "HARD FAILURES: " + (", ".join(hard_bad) if hard_bad else "none")]
    return "\n".join(lines)


def save(report: dict[str, Any]) -> Path:
    out = Path(__file__).parent / "results"
    out.mkdir(exist_ok=True)
    p = out / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    p.write_text(json.dumps(report, indent=1))
    return p
