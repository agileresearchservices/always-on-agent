"""python -m evals [--runs N] [--case NAME ...] [--concurrency K]   (needs ANTHROPIC_API_KEY; costs real tokens)"""
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
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.list:
        for c in CASES:
            print(f"{'hard' if c.hard else 'soft'}  {c.name:34} {c.description}")
        return 0
    cases = [c for c in CASES if not a.case or c.name in a.case]
    if not cases:
        print("no such case; try --list", file=sys.stderr)
        return 2
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set; this runs the real model (see .env.example)", file=sys.stderr)
        return 2
    from aoa.llm import AnthropicLLM

    report = asyncio.run(run_suite(cases, a.runs, AnthropicLLM, a.concurrency))
    print(format_table(report))
    print(f"\nresults: {save(report)}")
    return 1 if any(c["hard"] and c["passed"] != c["runs"] for c in report["cases"].values()) else 0


if __name__ == "__main__":
    sys.exit(main())
