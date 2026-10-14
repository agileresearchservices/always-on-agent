"""Getting events to a session's workflow, starting it if needed (signal-with-start).

Every trigger (HTTP message, webhook, routine, approval) funnels through deliver_event, so a
session workflow exists exactly when there is something for it to do, and never twice.
"""
from __future__ import annotations

from typing import Any

from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy

from .config import Settings


def session_workflow_id(session: str) -> str:
    return f"session-{session}"


def workflow_cfg(s: Settings) -> dict[str, Any]:
    """Everything a workflow needs to know, passed as input so replay never depends on env vars."""
    return {
        "idle_sleep": s.idle_sleep_seconds,
        "batch_window": s.batch_window_seconds,
        "max_steps": s.max_steps_per_turn,
        "max_helper_steps": s.max_helper_steps,
        "max_helper_depth": s.max_helper_depth,
        "shell_timeout": s.shell_timeout_seconds,
        "warm_min_gap": max(30, s.cache_ttl_seconds - 60),
    }


async def _signal_with_start(client: Client, s: Settings, session: str, signal: str, args: list[Any]) -> None:
    await client.start_workflow(
        "SessionWorkflow",
        {"session": session, "cfg": workflow_cfg(s), "carry": []},
        id=session_workflow_id(session),
        task_queue=s.task_queue,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        start_signal=signal,
        start_signal_args=args,
    )


async def deliver_event(client: Client, s: Settings, session: str, event: dict[str, Any]) -> None:
    await _signal_with_start(client, s, session, "submit_event", [event])


async def signal_typing(client: Client, s: Settings, session: str) -> None:
    await _signal_with_start(client, s, session, "typing", [])
