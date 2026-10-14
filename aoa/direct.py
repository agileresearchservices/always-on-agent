"""In-process runner: the same loop and core without Temporal.

Useful for tests and for trying the agent with no server. It is NOT durable; a crash loses
the in-flight turn (the SQLite state survives, so the next turn continues from it).
"""
from __future__ import annotations

import uuid
from typing import Any

from .core import AgentCore
from .loop import LoopResult, run_agent_loop


class DirectOps:
    def __init__(self, core: AgentCore, session: str):
        self.core, self.session = core, session

    async def llm_step(self, conv, step_key, role, depth):
        return await self.core.llm_step(self.session, conv, step_key, role, depth)

    async def run_tool(self, conv, role, depth, tool_use, trigger_user):
        return await self.core.run_tool(self.session, conv, role, depth, tool_use, trigger_user)

    async def append_results(self, conv, step_key, results, events):
        await self.core.append_results(self.session, conv, step_key, results, events)

    async def spawn_helper(self, conv, tool_use, depth):
        hconv = f"{conv}/helper/{tool_use['id']}"
        await self.core.prepare_helper(hconv, tool_use["input"]["task"])
        res = await run_agent_loop(
            DirectOps(self.core, self.session), conv=hconv, role="helper", depth=depth + 1, turn_key="h",
            max_steps=self.core.s.max_helper_steps, max_helper_depth=self.core.s.max_helper_depth,
            trigger_user=False,
        )
        return res.text or "(helper finished without a result)"


async def handle_events(
    core: AgentCore, session: str, events: list[dict[str, Any]], steering=None
) -> LoopResult | None:
    """One turn: prepare, loop, compact. Returns None if every event was a duplicate."""
    turn_key = uuid.uuid4().hex[:8]
    prep = await core.prepare_turn(session, session, turn_key, events)
    if not prep["added"]:
        return None
    result = await run_agent_loop(
        DirectOps(core, session), conv=session, role="main", depth=0, turn_key=turn_key,
        max_steps=core.s.max_steps_per_turn, max_helper_depth=core.s.max_helper_depth,
        trigger_user=prep["trigger_user"], steering=steering,
    )
    await core.maybe_compact(session, session)
    return result
