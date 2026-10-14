"""Merging a reviewed PR from Slack: what to do given GitHub's view of the PR, and what to tell the person.

Pure functions only (used by activities and tests). The merge itself is never a model tool: it starts from a
human click on a [Merge] button, which names an opaque merge offer that `publish_pr_review` stored for one PR
at one reviewed commit. The worker (which holds the GitHub token) does the rest in MergePrWorkflow.
"""
from __future__ import annotations

from typing import Any

from .github_monitor import slack_safe

OFFER_KEY = "merge-offer:"  # kv: merge-offer:<id> -> the offer
OFFER_FOR_KEY = "merge-offer-for:"  # kv: merge-offer-for:<repo>#<n>@<sha> -> <id>, so a retried publish reuses it
OFFER_ID_RE = r"mrg-[0-9a-f]{12}"

FAILED_CONCLUSIONS = {"failure", "cancelled", "timed_out", "action_required", "startup_failure"}
# outcomes after which clicking Merge again on the same card can succeed (same commit, state may change)
RETRYABLE = {"checks_failed", "waited_too_long", "blocked", "draft", "error", "refused"}


def check_summary(status: dict[str, Any], check_runs: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(failing, pending) check names for a commit, from the combined commit status and the check runs.

    The combined status reports "pending" when a commit has no statuses at all, so it only counts when
    total_count > 0. A repo with no CI has neither, and nothing is failing or pending."""
    failing, pending = [], []
    if status.get("total_count", 0) > 0:
        for s in status.get("statuses") or []:
            name = slack_safe(str(s.get("context", "status")))[:80]
            if s.get("state") in ("failure", "error"):
                failing.append(name)
            elif s.get("state") == "pending":
                pending.append(name)
    for c in check_runs.get("check_runs") or []:
        name = slack_safe(str(c.get("name", "check")))[:80]
        if c.get("status") != "completed":
            pending.append(name)
        elif c.get("conclusion") in FAILED_CONCLUSIONS:
            failing.append(name)
    return failing, pending


def decide(pr: dict[str, Any], sha: str, failing: list[str], pending: list[str]) -> tuple[str, str]:
    """(action, reason). action is one of: already_merged, closed, stale, draft, checks_failed, wait,
    conflict, update_branch, blocked, merge."""
    if pr.get("merged"):
        return "already_merged", "already merged"
    if pr.get("state") != "open":
        return "closed", "the PR was closed without merging"
    if (pr.get("head") or {}).get("sha") != sha:
        return "stale", ("new commits were pushed after the review; the new head gets its own review and "
                         "Merge button")
    if pr.get("draft"):
        return "draft", "the PR is a draft again"
    if failing:
        return "checks_failed", "failing checks: " + ", ".join(failing[:8])
    if pending:
        return "wait", "checks still running: " + ", ".join(pending[:8])
    state = pr.get("mergeable_state") or "unknown"
    if pr.get("mergeable") is None or state == "unknown":
        return "wait", "GitHub is still computing mergeability"
    if state == "dirty" or pr.get("mergeable") is False:
        return "conflict", "the branch has merge conflicts with the base; resolve them, then the new commit is re-reviewed"
    if state == "behind":
        return "update_branch", "the branch is behind the base"
    if state == "blocked":
        return "blocked", ("blocked by branch protection (for example a required approving review, which the "
                           "PR author's own account cannot give)")
    if state == "unstable":
        return "checks_failed", "GitHub reports failing non-required checks"
    return "merge", "ready"


def offer_head(offer: dict[str, Any]) -> str:
    return f"[{slack_safe(offer['repo'])}#{offer['number']} {slack_safe(offer.get('title', ''))}]({offer['url']})"


def report_text(offer: dict[str, Any], out: dict[str, Any]) -> str:
    """The final Slack notice for one merge attempt. `out` is MergePrWorkflow's outcome."""
    head, status = offer_head(offer), out["status"]
    if status == "merged":
        lines = [f":tada: *Merged* {head}"
                 + (f" as `{out['merge_sha'][:7]}` (squash)" if out.get("merge_sha") else "")]
        branch = out.get("branch")
        if branch:
            lines.append(f"Branch `{slack_safe(branch['ref'])}`: {branch['result']}.")
        issues = out.get("issues")
        if issues is not None:
            if not issues:
                lines.append(":warning: No linked issue (`Closes #N`), so nothing was auto-closed.")
            for i in issues:
                ref = f"{slack_safe(i['repo'])}#{i['number']}"
                lines.append(f"Issue {ref} closed." if i.get("state") == "CLOSED" else
                             f":warning: Issue {ref} is still {str(i.get('state', 'unknown')).lower()}.")
        if out.get("note"):
            lines.append(f"_{slack_safe(out['note'])}_")
        return "\n".join(lines)
    if status == "updated":
        return (f":arrows_counterclockwise: {head} was behind its base, so I updated the branch. The new commit "
                "will be reviewed again and get its own Merge button.")
    again = " Click Merge on the card to try again." if out.get("action") in RETRYABLE else ""
    return f":no_entry: *Not merged* {head}: {slack_safe(out.get('reason', 'unknown error'))}.{again}"
