"""Model access behind a tiny interface, plus the cache-breakpoint placement logic.

Responses are normalised to plain JSON-able dicts so they can cross Temporal activity
boundaries and sit in SQLite unchanged.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class LLMResponse:
    content: list[dict[str, Any]]
    stop_reason: str
    usage: dict[str, int]
    model: str

    def to_dict(self) -> dict[str, Any]:
        return {"content": self.content, "stop_reason": self.stop_reason, "usage": self.usage, "model": self.model}


class LLM(Protocol):
    async def create(
        self,
        *,
        model: str,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        tool_choice: dict[str, Any] | None = None,
        thinking: dict[str, Any] | None = None,
        effort: str | None = None,
    ) -> LLMResponse: ...


THINKING_TYPES = ("thinking", "redacted_thinking")


def ttl_label(seconds: int) -> str | None:
    """cache_control ttl for a lifetime in seconds: the API offers 5 minutes (the default, so no field) or 1 hour."""
    return "1h" if seconds > 300 else None


def place_cache_breakpoints(
    system: list[dict[str, Any]], messages: list[dict[str, Any]], tools: list[dict[str, Any]], ttl: str | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return copies with at most four breakpoints: last tool, last system block, last message block.

    The system blocks already carry their own breakpoint (see prompt.system_blocks). Here we add one
    on the final tool definition, and one on the final content block of the final message so that
    the whole history so far is cached and each new turn only pays for the delta.
    """
    system = copy.deepcopy(system)
    messages = copy.deepcopy(messages)
    tools = copy.deepcopy(tools)
    if tools:
        tools[-1]["cache_control"] = {"type": "ephemeral"}
    if system and not any("cache_control" in b for b in system):
        system[-1]["cache_control"] = {"type": "ephemeral"}
    # strip any stale markers inside history, then mark the very last block
    for m in messages:
        for b in m["content"]:
            b.pop("cache_control", None)
    if messages:  # a thinking block cannot carry a breakpoint: mark the last block that can
        for b in reversed(messages[-1]["content"]):
            if b["type"] not in THINKING_TYPES:
                b["cache_control"] = {"type": "ephemeral"}
                break
    if ttl:  # every breakpoint gets the same lifetime (longer ones must not follow shorter ones)
        for b in [*tools, *system, *(b for m in messages for b in m["content"])]:
            if "cache_control" in b:
                b["cache_control"] = {**b["cache_control"], "ttl": ttl}
    return system, messages, tools


class AnthropicLLM:
    def __init__(self, client: Any | None = None, cache_ttl_seconds: int | None = None):
        if client is None:
            import anthropic

            client = anthropic.AsyncAnthropic()
        self.client = client
        if cache_ttl_seconds is None:  # same source as the workflow's warm-up and miss-detection windows
            from .config import Settings

            cache_ttl_seconds = Settings.from_env().cache_ttl_seconds
        self.ttl = ttl_label(cache_ttl_seconds)

    async def create(
        self,
        *,
        model: str,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        tool_choice: dict[str, Any] | None = None,
        thinking: dict[str, Any] | None = None,
        effort: str | None = None,
    ) -> LLMResponse:
        system, messages, tools = place_cache_breakpoints(system, messages, tools, self.ttl)
        kwargs: dict[str, Any] = dict(model=model, max_tokens=max_tokens, system=system, messages=messages)
        if tools:
            kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
        if thinking:
            kwargs["thinking"] = thinking
        if effort:
            kwargs["output_config"] = {"effort": effort}
        resp = await self.client.messages.create(**kwargs)
        content: list[dict[str, Any]] = []
        for b in resp.content:
            if b.type == "text":
                content.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                content.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
            elif b.type == "thinking":  # kept verbatim: the API rejects a changed or reordered thinking block
                content.append({"type": "thinking", "thinking": b.thinking, "signature": b.signature})
            elif b.type == "redacted_thinking":
                content.append({"type": "redacted_thinking", "data": b.data})
        u = resp.usage
        usage = {
            "input_tokens": getattr(u, "input_tokens", 0) or 0,
            "output_tokens": getattr(u, "output_tokens", 0) or 0,
            "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
        }
        return LLMResponse(content=content, stop_reason=resp.stop_reason or "end_turn", usage=usage, model=model)
