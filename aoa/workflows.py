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
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ChildWorkflowError

with workflow.unsafe.imports_passed_through():
    from .loop import run_agent_loop

LLM_RETRY = RetryPolicy(
    maximum_attempts=6, initial_interval=timedelta(seconds=2), maximum_interval=timedelta(seconds=60),
    backoff_coefficient=2.0,
)
STATE_RETRY = RetryPolicy(maximum_attempts=5, initial_interval=timedelta(seconds=1))
NO_RETRY = RetryPolicy(maximum_attempts=1)


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

        while not self._stop:
            if not self._should_wake():
                if self._asleep:
                    await workflow.wait_condition(self._should_wake)
                else:
                    try:
                        await workflow.wait_condition(
                            self._should_wake, timeout=timedelta(seconds=cfg["idle_sleep"])
                        )
                    except asyncio.TimeoutError:
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

            if workflow.info().is_continue_as_new_suggested() and workflow.all_handlers_finished():
                workflow.continue_as_new(
                    {"session": session, "cfg": cfg, "carry": self._user_q + self._bg_q}
                )

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
