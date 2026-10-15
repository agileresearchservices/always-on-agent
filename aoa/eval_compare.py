"""Compare an eval run with the last green baseline (flywheel follow-up, #61). Pure functions: no I/O, no model.

A *summary* is the small, stable part of a run report kept between runs:
    {"ts": float, "fingerprint": str, "model": str, "runs": int, "est_cost_usd": float|None,
     "cases": {name: {"hard": bool, "passed": int, "runs": int}}}

A regression is either a hard case that did not pass every run (hard cases are safety rules and must stay green,
whatever the baseline says) or a soft case whose pass rate fell by more than `SOFT_DROP` against the *best* of the
recent green runs (a window, so a case that loses one run per step cannot slide away unnoticed).
"""
from __future__ import annotations

from typing import Any

SOFT_DROP = 0.34  # with 3 runs per case one lost run (-0.33) is noise, two (-0.67) is a regression


def summarize(report: dict[str, Any], *, ts: float, fingerprint: str) -> dict[str, Any]:
    return {
        "ts": ts, "fingerprint": fingerprint, "model": report.get("model", ""),
        "runs": report.get("runs_per_case", 0), "est_cost_usd": report.get("est_cost_usd"),
        "cases": {n: {"hard": bool(c["hard"]), "passed": int(c["passed"]), "runs": int(c["runs"])}
                  for n, c in report["cases"].items()},
    }


def _rate(c: dict[str, Any]) -> float:
    return c["passed"] / c["runs"] if c["runs"] else 0.0


def compare(baseline: dict[str, Any] | list[dict[str, Any]] | None, new: dict[str, Any],
            soft_drop: float = SOFT_DROP) -> list[dict[str, Any]]:
    """Regressions in `new`, sorted by case name. Each: {"case", "kind": "hard"|"soft", "detail"}.
    `baseline` is one summary or a list of green ones (oldest first); a soft case is held to its best rate in the list,
    and "missing from this run" is judged against the newest."""
    window = [] if baseline is None else baseline if isinstance(baseline, list) else [baseline]
    out: list[dict[str, Any]] = []
    base_cases = window[-1].get("cases", {}) if window else {}
    for name, b in sorted(base_cases.items()):  # a case that vanished from the run is not a pass
        if name not in new["cases"]:
            out.append({"case": name, "kind": "hard" if b["hard"] else "soft", "detail": "case missing from this run"})
    for name, c in sorted(new["cases"].items()):
        if c["hard"]:
            if c["passed"] != c["runs"] or c["runs"] == 0:
                out.append({"case": name, "kind": "hard", "detail": f"hard case passed {c['passed']}/{c['runs']} (must pass every run)"})
            continue
        seen = [w["cases"][name] for w in window if w["cases"].get(name, {}).get("runs")]
        b = max(seen, key=_rate) if seen else None
        if b and _rate(b) - _rate(c) > soft_drop + 1e-9:
            best = " (best of the recent green runs)" if len(seen) > 1 else ""
            out.append({"case": name, "kind": "soft",
                        "detail": f"soft case fell from {b['passed']}/{b['runs']}{best} to {c['passed']}/{c['runs']}"})
    return out


def signature(regressions: list[dict[str, Any]]) -> str:
    """Identity of a set of regressions, so the same failure is posted once and not at every scheduled run."""
    return ",".join(f"{r['kind']}:{r['case']}" for r in regressions)


def report_text(regressions: list[dict[str, Any]], new: dict[str, Any]) -> str:
    cost = new.get("est_cost_usd")
    lines = [f":rotating_light: *Eval regression* ({len(regressions)}) on the scheduled run of {new['runs']} run(s) per case"
             + (f", est. ${cost:.2f}" if cost else "")]
    lines += [f"• `{r['case']}`: {r['detail']}" for r in regressions]
    lines.append("The baseline stays at the last green run. Details: `python -m aoa evalruns`.")
    return "\n".join(lines)
