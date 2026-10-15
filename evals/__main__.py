"""python -m evals [--runs N] [--case NAME ...] [--concurrency K] [--batch] [--spec FILE] [--json]   (needs ANTHROPIC_API_KEY; costs real tokens)

Cases tagged "slow" (the hard review set) run only when named with --case or selected with --tag review-hard (or slow);
--tag review alone gives the quick seed review cases.

--batch sends every round of model calls through the Message Batches API: half the price, but each round takes
minutes instead of seconds. For big sample sizes, not for a quick check."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from .cases import CASES
from .runner import format_table, run_suite, save


def select_cases(cases, names=None, tags=None):
    """--case and --tag narrow each other; "slow" cases run only when named or asked for by tag."""
    return [c for c in cases
            if (not names or c.name in names) and (not tags or any(t in c.tags for t in tags))
            and (names or "slow" not in c.tags or {"slow", "review-hard"} & set(tags or []))]


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m evals")
    ap.add_argument("--runs", type=int, default=3, help="runs per case (default 3)")
    ap.add_argument("--case", action="append", help="only this case (repeatable)")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--batch", action="store_true", help="use the Message Batches API (50%% price, slower)")
    ap.add_argument("--batch-poll", type=float, default=15.0, help="seconds between batch status checks")
    ap.add_argument("--tag", action="append", help="only cases with this tag (repeatable); the slow hard review set needs --tag review-hard")
    ap.add_argument("--paged", action="store_true", help="review the old way: the reviewer fetches the PR itself and pages through the spilled diff (for comparison; production inlines it)")
    ap.add_argument("--second-pass", action="store_true", help="experiment: review each PR twice, the second pass hunting ordering/error-path/cross-file bugs")
    ap.add_argument("--spec", help="run the one case in this JSON spec file instead of the suite (used to validate a flywheel proposal)")
    ap.add_argument("--json", action="store_true", help="print the report as JSON on stdout instead of the table")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.list:
        for c in CASES:
            print(f"{'hard' if c.hard else 'soft'}  {c.name:34} {c.description}")
        return 0
    if a.spec:
        from .cases import Case
        from .spec import case_from_spec

        try:
            cases = [case_from_spec(json.loads(Path(a.spec).read_text()), Case)]
        except (OSError, ValueError) as e:  # SpecError is a ValueError, and so is bad JSON
            print(f"bad spec: {e}", file=sys.stderr)
            return 2
    else:
        cases = select_cases(CASES, a.case, a.tag)
    if not cases:
        print("no such case; try --list", file=sys.stderr)
        return 2
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set; this runs the real model (see .env.example)", file=sys.stderr)
        return 2
    from aoa.llm import AnthropicLLM
    from . import runner

    runner.SECOND_PASS = a.second_pass
    runner.INLINE_DIFF = not a.paged

    coord = None
    factory, concurrency = AnthropicLLM, a.concurrency
    if a.batch:
        from aoa.batch_llm import BatchCoordinator, BatchedLLM

        coord = BatchCoordinator(poll=a.batch_poll)
        factory, concurrency = (lambda: BatchedLLM(coord)), max(1, min(len(cases) * a.runs, 1000))  # all runs share each round (a batch holds <= 1000 requests here)
    report = asyncio.run(run_suite(cases, a.runs, factory, concurrency))
    if coord is not None:
        report["batch"] = {"batches": coord.batches, "requests": coord.requests}
    if a.json:
        print(json.dumps({k: report[k] for k in ("model", "runs_per_case", "est_cost_usd", "cases")}))
        return 0
    print(format_table(report))
    print(f"\nresults: {save(report)}")
    return 1 if any(c["hard"] and c["passed"] != c["runs"] for c in report["cases"].values()) else 0


if __name__ == "__main__":
    sys.exit(main())
