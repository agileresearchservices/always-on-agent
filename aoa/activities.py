"""Temporal activities: thin wrappers over AgentCore, one per durable step."""
from __future__ import annotations

from typing import Any

import httpx
from temporalio import activity
from temporalio.client import Client

from .config import Settings
from .core import AgentCore
from .delivery import deliver_event
from .events import Event
from .github_monitor import poll_once, token_provider


class AgentActivities:
    def __init__(self, core: AgentCore, client: Client, settings: Settings):
        self.core, self.client, self.s = core, client, settings

    @activity.defn(name="prepare_turn")
    async def prepare_turn(self, a: dict[str, Any]) -> dict[str, Any]:
        return await self.core.prepare_turn(a["session"], a["conv"], a["turn_key"], a["events"])

    @activity.defn(name="prepare_helper")
    async def prepare_helper(self, a: dict[str, Any]) -> None:
        await self.core.prepare_helper(a["conv"], a["task"])

    @activity.defn(name="llm_step")
    async def llm_step(self, a: dict[str, Any]) -> dict[str, Any]:
        return await self.core.llm_step(a["session"], a["conv"], a["step_key"], a["role"], a["depth"])

    @activity.defn(name="run_tool")
    async def run_tool(self, a: dict[str, Any]) -> dict[str, Any]:
        return await self.core.run_tool(
            a["session"], a["conv"], a["role"], a["depth"], a["tool_use"], a["trigger_user"]
        )

    @activity.defn(name="append_results")
    async def append_results(self, a: dict[str, Any]) -> None:
        await self.core.append_results(a["session"], a["conv"], a["step_key"], a["results"], a["events"])

    @activity.defn(name="maybe_compact")
    async def maybe_compact(self, a: dict[str, Any]) -> str:
        return await self.core.maybe_compact(a["session"], a["conv"])

    @activity.defn(name="sandbox_sleep")
    async def sandbox_sleep(self, session: str) -> str:
        return await self.core.sandbox_sleep(session)

    @activity.defn(name="warm_cache")
    async def warm_cache(self, a: dict[str, Any]) -> dict[str, int]:
        return await self.core.warm_cache(a["session"], a["conv"])

    @activity.defn(name="report_failure")
    async def report_failure(self, a: dict[str, Any]) -> None:
        self.core.store.outbox_push(a["session"], "system", {"text": a["text"]}, client_key=a.get("client_key"))

    @activity.defn(name="deliver_routine_event")
    async def deliver_routine_event(self, a: dict[str, Any]) -> None:
        ev = Event(
            source="routine", kind="routine", priority=1, trusted=True,
            text=f"[Routine '{a['name']}' fired]\n{a['prompt']}",
            dedupe_key=a["dedupe_key"],
        )
        await deliver_event(self.client, self.s, a["session"], ev.to_dict())

    @activity.defn(name="poll_github")
    async def poll_github(self, a: dict[str, Any]) -> int:
        async with httpx.AsyncClient(timeout=20) as http:
            events = await poll_once(http, self.core.store, a["session"], a["user"], token_provider())
        for ev in events:
            await deliver_event(self.client, self.s, a["session"], ev.to_dict())
        return len(events)

    def all(self) -> list:
        return [
            self.prepare_turn, self.prepare_helper, self.llm_step, self.run_tool, self.append_results,
            self.maybe_compact, self.sandbox_sleep, self.warm_cache, self.report_failure,
            self.deliver_routine_event, self.poll_github,
        ]
