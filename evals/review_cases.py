"""Hard review cases built from this repo's own PR history (see review_fixtures/build.py).

`pre` cases are PR commits whose review found real problems that were then fixed (the fixing commit or PR is
named next to each); the reviewer should name them. `clean` cases are the fixed commits the reviewer approved: no
high finding, and an approving verdict. Everything is soft: the pass rate and the recorded defect counts are
the measurement, and they are the baseline for any change to the reviewer (a verification pass, a model tier).

Needles are alternatives matched case-insensitively, each word starting at a word boundary ("hang" does not
match "change"), against one finding or the summary; "a&b" needs both words in the same finding. They are written from the real review findings and fix commits, loose enough for wording.
"""
from __future__ import annotations

import json
from pathlib import Path

from .graders import finds_defects, no_error, no_external_effects, no_high_findings, verdict_is

DIR = Path(__file__).parent / "review_fixtures"

# fixture -> (description, defect groups, how many the reviewer must name, fixed by)
PRE = {
    "pr12_review_flow": (
        "The first PR-review flow: verdict not enforced, queue lost on retry, schedule args after deploy.",
        {"verdict_not_enforced": ("approve&high", "verdict&injected", "verdict&findings&consistent", "downgrade"),
         "model_text_unescaped": ("no_mentions", "_where", "mention&slack", "channel&slack", "neutralis"),
         "queue_lost": ("seen&queue", "never reviewed", "cap&review", "dequeue", "queue&lost", "lost&pr"),
         "schedule_args_after_deploy": ("keyerror", "existing schedule", "cfg&schedule", "ensure_github_schedule")},
        2, "0801d7e"),
    "pr17_resolve_conflicts": (
        "Conflict resolution on Merge: runs PR-controlled test commands, symlinks, marker after push, protected heads.",
        {"pr_controlled_tests": ("test-command", "test command&branch", "test command&pr", "pip install&pr",
                                 "runs&pr", "arbitrary code", "as the worker"),
         "symlink_or_submodule": ("symlink", "submodule", "read_bytes&follow"),
         "marker_after_push": ("resolved_key", "marker&push", "written only after", "already&path"),
         "rejected_means_moved": ("rejected&moved", "non-fast-forward", "workflow scope", "workflow&token"),
         "protected_head": ("protected_refs", "protected&head", "protected branch"),
         "gutted_resolution": ("gutted", "drops one side", "silently drop", "empty or truncated", "half the shorter",
                               "survive&both sides")},
        3, "a04e82a, 5cdc0e1, a80b769"),
    "pr22_resolver_timeouts": (
        "Resolver test timeout: unparsed env value and a limit longer than the activity's.",
        {"unparsed_timeout": ("valueerror", "parse&defensive", "bad value", "invalid value", "float(&env"),
         "exceeds_activity_limit": ("start-to-close", "30 min", "activity limit", "activity&timeout", "deadline",
                                    "temporal&cut")},
        1, "#24"),
    "pr31_approval_expiry": (
        "Approval nudge and expiry: state changed before the notice is sent, stale clicks, config.",
        {"state_before_send": ("before&send", "before&notif", "marks&expired", "never told", "never notified",
                               "no longer pending"),
         "stale_click": ("late click", "click&expir", "stale&approv", "after the expiry", "past its lifetime"),
         "tick_under_load": ("never ticks", "starv", "tick&busy", "tick&load", "tick&loop"),
         "bad_config": ("nudge&expire", "clamp", "zero&expire", "misconfig", "expire&less than")},
        1, "9beb5fc"),
    "pr33_search_attributes": (
        "Search attributes: child start fails when they are not registered; stored offer read before the try.",
        {"unregistered_attrs_fail_child": ("not registered", "unregistered", "search attribute&fail",
                                           "ensure_registered", "child start&fail"),
         "fragile_error_match": ("fragile", "message text", "matching the text", "invalid_argument", "status code"),
         "offer_fields_before_try": ("keyerror", "before the try", "missing&offer", "500&offer")},
        2, "b7302a0"),
    "pr41_batch_evals": (
        "Batch eval driver: transient errors, cancelled futures, no chunking, in-flight callers hang, create retry.",
        {"transient_poll_errors": ("batches.retrieve", "batches.results", "network blip", "poll&retry", "transient&poll",
                                   "backoff", "polling&fail", "retrieve&error"),
         "cancelled_future": ("invalidstateerror", "done()", "cancelled&future", "future&cancel"),
         "no_chunking": ("chunk&batch", "chunk&round", "100k", "256 mb", "batch limits", "batch size", "request-count", "size limit", "api limit",
                         "every run at once"),
         "inflight_callers_hang": ("in-flight", "hang", "already taken out", "never failed", "never resolve", "never complete",
                                  "driver&cancel", "driver&dies"),
         "create_retry_duplicates": ("duplicate&batch", "second, billed", "lost response", "create&retr", "non-transient")},
        3, "5d4c75b, 9745fda"),
}
CLEAN = {
    "pr31_approval_expiry_fixed": "Approval expiry after its review fixes.",
    "pr33_search_attributes_fixed": "Search attributes after their review fixes.",
    "pr41_batch_evals_fixed": "Batch evals after their review fixes.",
    "pr43_reviewer_upgrades_fixed": "The reviewer upgrades after their review fixes.",
}


def _fixture(name: str) -> dict:
    meta = json.loads((DIR / name / "meta.json").read_text())
    return {"title": meta["title"], "body": meta["body"] or "", "diff": (DIR / name / "diff.patch").read_text()}


def hard_review_cases(Case):
    cases = []
    for name, (desc, groups, need, fixed_by) in PRE.items():
        cases.append(Case(
            f"hardreview_{name}", f"{desc} (fixed in {fixed_by})", [],
            [no_error(), no_external_effects(), finds_defects(groups, need)],
            hard=False, mode="review", pr_fixture=_fixture(name), tags=["review", "review-hard", "slow"]))
    for name, desc in CLEAN.items():
        cases.append(Case(
            f"hardreview_{name}", f"{desc} Approved by the reviewer once; it should not block it now.", [],
            [no_error(), no_external_effects(), verdict_is("approve"), no_high_findings()],
            hard=False, mode="review", pr_fixture=_fixture(name), tags=["review", "review-hard", "slow"]))
    return cases
