"""Temporal workflows.

SessionWorkflow  : one long-lived workflow per session. Owns the queue, the idle->sleep timer and the
                   turn loop. Every model call and tool call is an activity, so a crash resumes at the
                   last completed step instead of replaying the turn (the "durable workflow vs job
                   queue" distinction from the lecture).
HelperWorkflow   : a child workflow per spawn_helper call, with its own conversation.
RoutineWorkflow  : fired by a Temporal Schedule; hands a prompt to the session.

Workflow code must stay deterministic: no I/O, no clocks, no randomness except workflow.* helpers.
All configuration arrives as workflow input.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ActivityError, ChildWorkflowError, WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    from .loop import run_agent_loop
    from .pr_review import build_task

LLM_RETRY = RetryPolicy(
    maximum_attempts=6, initial_interval=timedelta(seconds=2), maximum_interval=timedelta(seconds=60),
    backoff_coefficient=2.0,
)
STATE_RETRY = RetryPolicy(maximum_attempts=5, initial_interval=timedelta(seconds=1))
NO_RETRY = RetryPolicy(maximum_attempts=1)
CUT_OFF = "[helper output was cut off at the token limit]"


class WorkflowOps:
    """Loop operations implemented as activities and child workflows."""

    def __init__(self, session: str, cfg: dict[str, Any]):
        self.session, self.cfg = session, cfg

    async def llm_step(self, conv, step_key, role, depth):
        return await workflow.execute_activity(
            "llm_step",
            {"session": self.session, "conv": conv, "step_key": step_key, "role": role, "depth": depth},
            start_to_close_timeout=timedelta(minutes=5), retry_policy=LLM_RETRY,
        )

    async def run_tool(self, conv, role, depth, tool_use, trigger_user):
        # At most once: a tool may have side effects, so it is never blindly retried. If it did not
        # complete, the model is told the effects are unknown and decides what to do.
        try:
            return await workflow.execute_activity(
                "run_tool",
                {"session": self.session, "conv": conv, "role": role, "depth": depth,
                 "tool_use": tool_use, "trigger_user": trigger_user},
                start_to_close_timeout=timedelta(seconds=self.cfg["shell_timeout"] + 60),
                retry_policy=NO_RETRY,
            )
        except ActivityError:
            return {
                "tool_use_id": tool_use["id"], "is_error": True,
                "content": f"Error: {tool_use['name']} did not complete (worker failure or timeout). Its "
                           "effects are unknown; check the current state before retrying.",
            }

    async def append_results(self, conv, step_key, results, events):
        await workflow.execute_activity(
            "append_results",
            {"session": self.session, "conv": conv, "step_key": step_key, "results": results, "events": events},
            start_to_close_timeout=timedelta(seconds=60), retry_policy=STATE_RETRY,
        )

    async def spawn_helper(self, conv, tool_use, depth):
        return await workflow.execute_child_workflow(
            "HelperWorkflow",
            {"session": self.session, "parent_conv": conv, "tool_use": tool_use, "depth": depth + 1, "cfg": self.cfg},
            id=f"helper-{self.session}-{tool_use['id']}",
            retry_policy=NO_RETRY,
            execution_timeout=timedelta(hours=4),
        )


@workflow.defn(name="SessionWorkflow")
class SessionWorkflow:
    def __init__(self) -> None:
        self._user_q: list[dict[str, Any]] = []
        self._bg_q: list[dict[str, Any]] = []
        self._seen: list[str] = []
        self._stop = False
        self._asleep = False
        self._warm_requested = False
        self._last_warm = -1e9
        self._flush_at: float | None = None  # when the held digest may go out (epoch seconds)
        self._apr_at: float | None = None  # when a pending approval next needs a nudge or lapses

    # ---- signals / queries ------------------------------------------------------------
    @workflow.signal
    def submit_event(self, ev: dict[str, Any]) -> None:
        key = ev.get("dedupe_key")
        if key:
            if key in self._seen:
                return
            self._seen.append(key)
            del self._seen[:-500]
        (self._user_q if ev.get("priority", 1) == 0 else self._bg_q).append(ev)

    @workflow.signal
    def typing(self) -> None:
        self._warm_requested = True

    @workflow.signal
    def stop(self) -> None:
        self._stop = True

    @workflow.query
    def status(self) -> dict[str, Any]:
        return {"queued_user": len(self._user_q), "queued_background": len(self._bg_q), "asleep": self._asleep}

    # ---- helpers ------------------------------------------------------------------------
    def _has_work(self) -> bool:
        return bool(self._user_q or self._bg_q)

    def _should_wake(self) -> bool:
        return self._has_work() or self._stop or self._warm_requested

    def _drain_user(self) -> list[dict[str, Any]]:
        out, self._user_q = self._user_q, []
        return out

    # ---- main loop ------------------------------------------------------------------------
    @workflow.run
    async def run(self, inp: dict[str, Any]) -> None:
        session, cfg = inp["session"], inp["cfg"]
        for ev in inp.get("carry", []):
            self.submit_event(ev)
        ops = WorkflowOps(session, cfg)
        await self._arm_timers(session)  # items held / pending before a continue-as-new still need their timers

        while not self._stop:
            if not self._should_wake():
                idle = None if self._asleep else float(cfg["idle_sleep"])
                nxt = [t for t in (self._flush_at, self._apr_at) if t is not None]
                due = max(1.0, min(nxt) - workflow.time()) if nxt else None
                wait = idle if due is None else (due if idle is None else min(idle, due))
                try:
                    if wait is None:
                        await workflow.wait_condition(self._should_wake)
                    else:
                        await workflow.wait_condition(self._should_wake, timeout=timedelta(seconds=wait))
                except asyncio.TimeoutError:
                    fired = False
                    if self._flush_at is not None and workflow.time() >= self._flush_at:
                        await self._flush_digest(session)
                        fired = True
                    if self._apr_at is not None and workflow.time() >= self._apr_at:
                        res = await workflow.execute_activity(
                            "approvals_tick", session, start_to_close_timeout=timedelta(seconds=30),
                            retry_policy=STATE_RETRY,
                        )
                        self._apr_at = res.get("next")
                        fired = True
                    if fired:
                        continue
                    await workflow.execute_activity(
                        "sandbox_sleep", session, start_to_close_timeout=timedelta(minutes=2),
                        retry_policy=STATE_RETRY,
                    )
                    self._asleep = True
                    continue
            if self._stop:
                break

            if self._warm_requested and not self._has_work():
                self._warm_requested = False
                if workflow.time() - self._last_warm >= cfg["warm_min_gap"]:
                    self._last_warm = workflow.time()
                    try:  # best effort; never worth failing the session over
                        await workflow.execute_activity(
                            "warm_cache", {"session": session, "conv": session},
                            start_to_close_timeout=timedelta(seconds=60), retry_policy=NO_RETRY,
                        )
                    except ActivityError:
                        pass
                continue
            self._warm_requested = False
            if not self._has_work():
                continue

            self._asleep = False
            if not self._user_q:
                await workflow.sleep(cfg["batch_window"])  # let a burst of background events coalesce
            if self._user_q:
                events, self._user_q = self._user_q, []  # the person always goes first
            else:
                events, self._bg_q = self._bg_q, []
            await self._run_turn(ops, session, cfg, events)
            await self._arm_timers(session)

            if workflow.info().is_continue_as_new_suggested() and workflow.all_handlers_finished():
                workflow.continue_as_new(
                    {"session": session, "cfg": cfg, "carry": self._user_q + self._bg_q}
                )

    async def _arm_timers(self, session: str) -> None:
        """Ask when held updates may go out and when a pending approval needs a nudge or lapses, so durable
        timers (not the next message) act on them."""
        if workflow.patched("digest-flush"):
            self._flush_at = await workflow.execute_activity(
                "digest_status", session, start_to_close_timeout=timedelta(seconds=30), retry_policy=STATE_RETRY,
            )
        if workflow.patched("approval-expiry"):
            self._apr_at = await workflow.execute_activity(
                "approvals_next", session, start_to_close_timeout=timedelta(seconds=30), retry_policy=STATE_RETRY,
            )

    async def _flush_digest(self, session: str) -> None:
        res = await workflow.execute_activity(
            "flush_digest", session, start_to_close_timeout=timedelta(seconds=30), retry_policy=STATE_RETRY,
        )
        self._flush_at = res.get("flush_at")

    async def _run_turn(self, ops: WorkflowOps, session: str, cfg: dict[str, Any], events: list[dict[str, Any]]) -> None:
        turn_key = workflow.uuid4().hex[:8]
        try:
            prep = await workflow.execute_activity(
                "prepare_turn",
                {"session": session, "conv": session, "turn_key": turn_key, "events": events},
                start_to_close_timeout=timedelta(seconds=60), retry_policy=STATE_RETRY,
            )
            if not prep["added"]:
                return
            await run_agent_loop(
                ops, conv=session, role="main", depth=0, turn_key=turn_key,
                max_steps=cfg["max_steps"], max_helper_depth=cfg["max_helper_depth"],
                trigger_user=prep["trigger_user"], steering=self._drain_user,
            )
            await workflow.execute_activity(
                "maybe_compact", {"session": session, "conv": session},
                start_to_close_timeout=timedelta(minutes=5), retry_policy=NO_RETRY,
            )
        except (ActivityError, ChildWorkflowError) as e:
            # Retries are exhausted. Keep the session alive and tell the person plainly.
            workflow.logger.error("turn %s failed: %s", turn_key, e)
            await workflow.execute_activity(
                "report_failure",
                {"session": session, "client_key": f"fail:{turn_key}",
                 "text": "I hit an error and had to stop before finishing. Send a message to have me pick it up again."},
                start_to_close_timeout=timedelta(seconds=30), retry_policy=STATE_RETRY,
            )


@workflow.defn(name="HelperWorkflow")
class HelperWorkflow:
    @workflow.run
    async def run(self, inp: dict[str, Any]) -> str:
        session, cfg, tu = inp["session"], inp["cfg"], inp["tool_use"]
        conv = f"{inp['parent_conv']}/helper/{tu['id']}"
        await workflow.execute_activity(
            "prepare_helper", {"conv": conv, "task": tu["input"]["task"]},
            start_to_close_timeout=timedelta(seconds=60), retry_policy=STATE_RETRY,
        )
        res = await run_agent_loop(
            WorkflowOps(session, cfg), conv=conv, role="helper", depth=inp["depth"], turn_key="h",
            max_steps=cfg["max_helper_steps"], max_helper_depth=cfg["max_helper_depth"], trigger_user=False,
        )
        text = res.text or "(helper finished without a result)"
        if res.stop == "max_steps":
            text = "[helper hit its step limit; this is a partial result]\n" + text
        elif res.stop == "max_tokens":
            text = CUT_OFF + "\n" + text
        return text[:6000]


@workflow.defn(name="RoutineWorkflow")
class RoutineWorkflow:
    @workflow.run
    async def run(self, inp: dict[str, Any]) -> None:
        await workflow.execute_activity(
            "deliver_routine_event",
            {"session": inp["session"], "name": inp["name"], "prompt": inp["prompt"],
             "dedupe_key": f"routine:{workflow.info().workflow_id}"},  # id includes the scheduled time
            start_to_close_timeout=timedelta(seconds=60), retry_policy=STATE_RETRY,
        )


@workflow.defn(name="GithubPollWorkflow")
class GithubPollWorkflow:
    """Fired by the monitor schedule. Polls, then starts one PrReviewWorkflow per PR to review. The reviews
    are abandoned children: they outlive this run, so a slow review never blocks the next poll."""

    @workflow.run
    async def run(self, inp: dict[str, Any]) -> int:
        res = await workflow.execute_activity(
            "poll_github", {"session": inp["session"], "user": inp["user"]},
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=5)),
        )
        if isinstance(res, int):  # recorded by a pre-review worker before a deploy: replay it as-is
            return res
        for pr in res["review"]:
            wid = f"pr-review-{inp['session']}-{pr['repo'].replace('/', '-')}-{pr['number']}-{pr['sha'][:12]}"
            try:
                await workflow.start_child_workflow(
                    "PrReviewWorkflow", {"session": inp["session"], "cfg": inp["cfg"], "pr": pr}, id=wid,
                    parent_close_policy=workflow.ParentClosePolicy.ABANDON,
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                )
            except WorkflowAlreadyStartedError:
                pass  # this commit was already reviewed (or is being reviewed)
        return res["announced"]


@workflow.defn(name="PrReviewWorkflow")
class PrReviewWorkflow:
    """One review of one PR at one commit: the reviewer sub-agent (HelperWorkflow), then publish."""

    @workflow.run
    async def run(self, inp: dict[str, Any]) -> dict[str, Any]:
        pr, session = inp["pr"], inp["session"]
        text, note = "", ""
        try:
            text = await workflow.execute_child_workflow(
                "HelperWorkflow",
                {"session": session, "cfg": inp["cfg"], "depth": 1,
                 "parent_conv": f"prreview/{pr['repo']}#{pr['number']}@{pr['sha'][:12]}",
                 "tool_use": {"id": "review", "name": "spawn_helper", "input": {"task": build_task(pr)}}},
                id=f"{workflow.info().workflow_id}-reviewer",
            )
        except ChildWorkflowError as e:
            note = f"Reviewer failed: {type(e.cause).__name__ if e.cause else 'error'}."
        if text.startswith(CUT_OFF):
            note = "The reviewer's answer was cut off at the output token limit (AOA_MAX_TOKENS)."
        return await workflow.execute_activity(
            "publish_pr_review", {"session": session, "pr": pr, "review_text": text, "note": note},
            start_to_close_timeout=timedelta(seconds=60), retry_policy=STATE_RETRY,
        )


# GitHub calls: retried for 5xx/network; scope violations, 401/403/404 (GithubError) will not get better
GH_RETRY = RetryPolicy(maximum_attempts=5, initial_interval=timedelta(seconds=2), maximum_interval=timedelta(seconds=30),
                       non_retryable_error_types=["GithubError"])


@workflow.defn(name="MergePrWorkflow")
class MergePrWorkflow:
    """Started by the server when the person clicks [Merge] on an approved review card; never by the model.

    Inspect (waiting for running checks / mergeability), then squash-merge pinned to the reviewed commit,
    delete the branch, confirm the linked issues closed, and report. A branch that is merely behind its base
    is updated instead; the new commit is re-reviewed and gets its own Merge button. Conflicts are stage 3."""

    async def _act(self, name: str, arg: Any) -> Any:
        return await workflow.execute_activity(
            name, arg, start_to_close_timeout=timedelta(seconds=60), retry_policy=GH_RETRY,
        )

    @workflow.run
    async def run(self, inp: dict[str, Any]) -> dict[str, Any]:
        offer = inp["offer"]
        pr = {"repo": offer["repo"], "number": offer["number"], "sha": offer["sha"]}
        out: dict[str, Any] = {"status": "failed"}
        try:
            deadline = workflow.time() + inp.get("wait_seconds", 600)
            while True:
                st = await self._act("merge_inspect", pr)
                if st["action"] != "wait" or workflow.time() >= deadline:
                    break
                await workflow.sleep(inp.get("poll_seconds", 20))
            out |= {"action": st["action"], "reason": st["reason"]}
            if st["action"] == "wait":
                out |= {"action": "waited_too_long", "reason": f"gave up waiting: {st['reason']}"}
            elif st["action"] == "conflict" and workflow.patched("resolve-conflicts"):
                # step one of two: resolve and push; the new commit is reviewed and merged by a second click
                res = await workflow.execute_activity(
                    "resolve_conflicts",
                    {"session": inp["session"], "repo": pr["repo"], "number": pr["number"], "sha": pr["sha"],
                     "head_ref": st["head_ref"],
                     "base_ref": st["base_ref"], "same_repo": st["same_repo"],
                     "protected_refs": st.get("protected_refs", [])},
                    start_to_close_timeout=timedelta(minutes=30), heartbeat_timeout=timedelta(minutes=2),
                    retry_policy=RetryPolicy(maximum_attempts=2, non_retryable_error_types=["GithubError"]),
                )
                out |= res
            elif st["action"] == "update_branch":
                res = await self._act("merge_update_branch", pr)
                out |= {"status": "updated"} if res["ok"] else {"action": "refused", "reason": res["reason"]}
            elif st["action"] in ("merge", "already_merged"):
                res = (await self._act("merge_pr", pr | {"title": st["title"]}) if st["action"] == "merge"
                       else {"merged": True, "merge_sha": st["merge_sha"] or ""})
                if not res["merged"]:
                    out |= {"action": "refused", "reason": res["reason"]}
                else:
                    out |= {"status": "merged", "merge_sha": res["merge_sha"]}
                    try:  # the merge is done; a close-out failure must not hide that
                        out["branch"] = await self._act("merge_delete_branch", {
                            "repo": pr["repo"], "ref": st["head_ref"], "same_repo": st["same_repo"],
                            "protected_refs": st.get("protected_refs", [])})
                        out |= await self._closeout_issues(st, inp.get("issue_poll_seconds", 5))
                    except ActivityError as e:
                        out["note"] = (f"Close-out failed ({type(e.cause).__name__ if e.cause else 'error'}): "
                                       "check the branch and linked issues by hand.")
        except ActivityError as e:
            out |= {"action": "error", "reason": f"a GitHub step failed ({type(e.cause).__name__ if e.cause else 'error'})"}
        await workflow.execute_activity(
            "merge_report",
            {"session": inp["session"], "offer_id": inp["offer_id"], "offer": offer, "card_ts": inp.get("card_ts"),
             "outcome": out, "run_id": workflow.info().run_id},
            start_to_close_timeout=timedelta(seconds=30), retry_policy=STATE_RETRY,
        )
        return out

    async def _closeout_issues(self, st: dict[str, Any], every: float) -> dict[str, Any]:
        """GitHub closes `Closes #N` issues asynchronously after the merge: look a few times."""
        issues = st["issues"]
        if issues is None:
            return {"note": "Could not read the PR's linked issues."}
        note = "" if st["default_base"] else "The base is not the default branch, so closing keywords do not fire."
        for attempt in range(4):
            if not issues or all(i["state"] == "CLOSED" for i in issues):
                break
            await workflow.sleep(every * (attempt + 1))
            issues = await self._act("merge_issue_states", issues)
        return {"issues": issues, "note": note}
