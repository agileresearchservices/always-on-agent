"""The agent loop: a model calling tools until it stops.

This is the same ten-line idea as the single-process harness, with three additions:
* every model call and every tool call is a separate durable step (an `Ops` method),
* new user events are steered into the conversation between steps instead of queueing,
* spawn_helper tool calls run as helpers with fresh context, in parallel.

`Ops` is implemented by Temporal activities/child workflows (workflows.py) and by plain calls
(direct.py). This module has no I/O, so it is deterministic and safe inside a workflow.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Protocol


class Ops(Protocol):
    async def llm_step(self, conv: str, step_key: str, role: str, depth: int) -> dict[str, Any]: ...
    async def run_tool(
        self, conv: str, role: str, depth: int, tool_use: dict[str, Any], trigger_user: bool
    ) -> dict[str, Any]: ...
    async def append_results(
        self, conv: str, step_key: str, results: list[dict[str, Any]], events: list[dict[str, Any]]
    ) -> None: ...
    async def spawn_helper(self, conv: str, tool_use: dict[str, Any], depth: int) -> str: ...


@dataclass
class LoopResult:
    text: str
    steps: int
    stop: str  # "end_turn" | "max_steps"


def _err(tool_use_id: str, msg: str) -> dict[str, Any]:
    return {"tool_use_id": tool_use_id, "content": f"Error: {msg}", "is_error": True}


async def run_agent_loop(
    ops: Ops,
    *,
    conv: str,
    role: str,
    depth: int,
    turn_key: str,
    max_steps: int,
    max_helper_depth: int,
    trigger_user: bool,
    steering: Callable[[], list[dict[str, Any]]] | None = None,
) -> LoopResult:
    last_text = ""
    for step in range(max_steps):
        step_key = f"{turn_key}.s{step}"
        resp = await ops.llm_step(conv, step_key, role, depth)
        if resp["text"]:
            last_text = resp["text"]
        tool_uses = resp["tool_uses"]
        if not tool_uses:
            return LoopResult(last_text, step + 1, "end_turn")

        if resp["stop_reason"] == "max_tokens":
            # the call was cut off mid-output; the tool input may be incomplete, so do not run it
            results = [
                _err(tu["id"], "your output was cut off before this call completed, so it was not run. "
                               "Retry with less in one call (write big content to a file in pieces).")
                for tu in tool_uses
            ]
        else:
            results = await _run_tools(ops, conv, role, depth, tool_uses, trigger_user, max_helper_depth)

        events = steering() if steering else []
        await ops.append_results(conv, step_key, results, events)
    return LoopResult(last_text, max_steps, "max_steps")


async def _run_tools(
    ops: Ops, conv: str, role: str, depth: int, tool_uses: list[dict[str, Any]], trigger_user: bool,
    max_helper_depth: int,
) -> list[dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    helper_tasks: dict[str, asyncio.Future[str]] = {}
    try:
        for tu in tool_uses:  # start helpers first so they overlap with everything else
            if tu["name"] != "spawn_helper":
                continue
            if role != "main" or depth >= max_helper_depth:
                results[tu["id"]] = _err(tu["id"], "helpers cannot spawn helpers")
            elif not (tu.get("input") or {}).get("task"):
                results[tu["id"]] = _err(tu["id"], "spawn_helper needs a 'task' with complete instructions")
            else:
                helper_tasks[tu["id"]] = asyncio.ensure_future(ops.spawn_helper(conv, tu, depth))
        for tu in tool_uses:
            if tu["name"] != "spawn_helper":
                results[tu["id"]] = await ops.run_tool(conv, role, depth, tu, trigger_user)
        for tid, task in helper_tasks.items():
            try:
                text = await task
                results[tid] = {"tool_use_id": tid, "content": text, "is_error": False}
            except Exception as e:  # noqa: BLE001
                results[tid] = _err(tid, f"helper failed ({type(e).__name__}). Retry once or do the work yourself.")
    finally:
        for task in helper_tasks.values():
            if not task.done():
                task.cancel()
    return [results[tu["id"]] for tu in tool_uses]
