"""python -m evals [--runs N] [--case NAME ...] [--concurrency K] [--batch]   (needs ANTHROPIC_API_KEY; costs real tokens)

Cases tagged "slow" (the hard review set, `--tag review-hard`) are left out of a default run.

--batch sends every round of model calls through the Message Batches API: half the price, but each round takes
minutes instead of seconds. For big sample sizes, not for a quick check."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from .cases import CASES
from .runner import format_table, run_suite, save


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m evals")
    ap.add_argument("--runs", type=int, default=3, help="runs per case (default 3)")
    ap.add_argument("--case", action="append", help="only this case (repeatable)")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--batch", action="store_true", help="use the Message Batches API (50%% price, slower)")
    ap.add_argument("--batch-poll", type=float, default=15.0, help="seconds between batch status checks")
    ap.add_argument("--tag", action="append", help="only cases with this tag (repeatable); \"slow\" cases run only when named or tagged")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.list:
        for c in CASES:
            print(f"{'hard' if c.hard else 'soft'}  {c.name:34} {c.description}")
        return 0
    cases = [c for c in CASES if (c.name in a.case if a.case else
                                  (any(t in c.tags for t in a.tag) if a.tag else "slow" not in c.tags))]
    if not cases:
        print("no such case; try --list", file=sys.stderr)
        return 2
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set; this runs the real model (see .env.example)", file=sys.stderr)
        return 2
    from aoa.llm import AnthropicLLM

    coord = None
    factory, concurrency = AnthropicLLM, a.concurrency
    if a.batch:
        from aoa.batch_llm import BatchCoordinator, BatchedLLM

        coord = BatchCoordinator(poll=a.batch_poll)
        factory, concurrency = (lambda: BatchedLLM(coord)), max(1, min(len(cases) * a.runs, 1000))  # all runs share each round (a batch holds <= 1000 requests here)
    report = asyncio.run(run_suite(cases, a.runs, factory, concurrency))
    if coord is not None:
        report["batch"] = {"batches": coord.batches, "requests": coord.requests}
    print(format_table(report))
    print(f"\nresults: {save(report)}")
    return 1 if any(c["hard"] and c["passed"] != c["runs"] for c in report["cases"].values()) else 0


if __name__ == "__main__":
    sys.exit(main())
