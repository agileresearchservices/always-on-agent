"""Replay recorded workflow histories against the current workflow code (no Temporal server needed).

A failure here means a worker running today's code could not resume workflows recorded by an older worker. Fix the
workflow change (guard it with workflow.patched / keep the command order), do not re-record the history. To cover
a NEW path, add a scenario to tests/histories/capture.py and record it. See tests/histories/capture.py.
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from temporalio import workflow
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from aoa.workflows import (
    EvalProposalWorkflow, GithubPollWorkflow, HelperWorkflow, MergePrWorkflow, PrReviewWorkflow, RoutineWorkflow,
    SessionWorkflow,
)

HISTORIES = sorted((Path(__file__).parent / "histories").glob("*.json"))
FLOWS = [SessionWorkflow, HelperWorkflow, RoutineWorkflow, GithubPollWorkflow, PrReviewWorkflow, MergePrWorkflow,
         EvalProposalWorkflow]


def test_histories_are_present():
    names = {p.stem for p in HISTORIES}
    assert {"session_timers_and_reply", "legacy_session_pre_timers", "pr_review", "github_poll",
            "legacy_poll_pre_search_attrs", "merge_wait_then_merge", "merge_update_branch",
            "merge_conflict_resolved", "pr_review_inline_diff", "eval_proposal"} <= names


@pytest.mark.parametrize("path", HISTORIES, ids=lambda p: p.stem)
async def test_history_replays(path: Path):
    d = json.loads(path.read_text())
    await Replayer(workflows=FLOWS).replay_workflow(WorkflowHistory.from_json(d["workflow_id"], d["history"]))


@workflow.defn(name="MergePrWorkflow")
class _ChangedMergeWorkflow:
    @workflow.run
    async def run(self, inp: dict) -> dict:
        await workflow.execute_activity("a_step_that_was_not_recorded", inp, start_to_close_timeout=timedelta(seconds=5))
        return {}


async def test_replay_catches_a_nondeterministic_change():
    """The harness itself: a workflow that issues different commands than the recorded history fails to replay."""
    d = json.loads((Path(__file__).parent / "histories" / "merge_wait_then_merge.json").read_text())
    with pytest.raises(Exception, match="(?i)determinis"):
        await Replayer(workflows=[_ChangedMergeWorkflow]).replay_workflow(
            WorkflowHistory.from_json(d["workflow_id"], d["history"]))


@pytest.mark.parametrize("path", HISTORIES, ids=lambda p: p.stem)
def test_histories_hold_no_secrets_or_local_paths(path: Path):
    body = path.read_text()
    for needle in ("/Users/", "/private/", "/var/folders", "sk-ant", "ghp_", "github_pat", "xoxb-", "xoxp-", "PRIVATE KEY"):
        assert needle not in body, f"{path.name} contains {needle!r}"
