"""Test doubles. FakeLLM replays a script so the whole stack runs without an API key."""
from __future__ import annotations

import itertools
from typing import Any, Callable

from .llm import LLMResponse

_ids = itertools.count(1)


def text(t: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": t}]


def tool(name: str, /, **inp: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": f"toolu_fake{next(_ids)}", "name": name, "input": inp}


Step = list[dict[str, Any]] | Callable[[dict[str, Any]], list[dict[str, Any]]] | Exception


class FakeLLM:
    """Each call pops the next scripted step. A step is content blocks, a callable(call)->blocks,
    or an Exception to raise. Calls are recorded for assertions."""

    def __init__(self, script: list[Step], usage: dict[str, int] | None = None):
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.usage = usage or {"input_tokens": 100, "output_tokens": 20,
                               "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}

    async def create(self, *, model, system, messages, tools, max_tokens, tool_choice=None, thinking=None, effort=None,
                     output_format=None) -> LLMResponse:
        call = {"model": model, "system": system, "messages": messages, "tools": tools, "tool_choice": tool_choice,
                "thinking": thinking, "effort": effort,
                "output_format": output_format}
        self.calls.append(call)
        if not self.script:
            raise AssertionError("FakeLLM script exhausted")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        out = step(call) if callable(step) else step
        stop = None
        if isinstance(out, dict):  # {"content": [...], "stop_reason": "max_tokens"}
            content, stop = out["content"], out.get("stop_reason")
        else:
            content = out
        stop = stop or ("tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn")
        return LLMResponse(content=content, stop_reason=stop, usage=dict(self.usage), model=model)


class MemoryScheduler:
    """In-memory stand-in for Temporal schedules."""

    def __init__(self):
        self.items: dict[tuple[str, str], tuple[str, str]] = {}

    async def create(self, session: str, name: str, cron: str, prompt: str) -> None:
        self.items[(session, name)] = (cron, prompt)

    async def delete(self, session: str, name: str) -> bool:
        return self.items.pop((session, name), None) is not None
