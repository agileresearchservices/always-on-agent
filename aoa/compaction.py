"""Context compaction.

Two triggers:
* hard : history is near the context limit, compact now.
* warm : history is large and the provider cache is still warm. Compacting *inside* the warm
         window is cheaper because the summarisation call reuses the cached prefix; the person
         then returns later to a short, cheap conversation.

The summariser call deliberately reuses the exact system prompt and tool list of the main agent
(same cache prefix), appends one instruction, and forbids tool use.
"""
from __future__ import annotations

import json
import time
from typing import Any

from .config import Settings
from .llm import LLM
from .store import Store

SUMMARY_REQUEST = """Compaction time. Write a summary of the conversation so far that will REPLACE it.
All compression is lossy, so be deliberate about what survives. Use exactly these sections:

1. Person: durable facts and preferences that affect how you should behave.
2. Goals and decisions: what is being worked on and what was decided, with the reason.
3. Open items: unfinished work, pending approvals, things promised, with owners and deadlines.
4. Exact identifiers: file paths, ids, names, numbers and commands that must not be paraphrased.
5. State of the world: what you already did (so you do not redo it) and what you learned.

Do not call tools. Do not address the person. Output only the summary."""


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    return len(json.dumps(messages)) // 4


def find_cut(indexed: list[tuple[int, dict[str, Any]]]) -> int | None:
    """Index of the last plain-text user message (a turn start). Everything before it is summarised.

    Messages that carry tool_result blocks are never cut points; cutting there would orphan a
    tool_use from its result.
    """
    for idx, m in reversed(indexed):
        if m["role"] != "user":
            continue
        kinds = {b["type"] for b in m["content"]}
        if "tool_result" not in kinds and "text" in kinds:
            return idx if idx >= 2 else None
    return None


def decide(tokens: int, seconds_since_last_call: float | None, s: Settings) -> str | None:
    if tokens >= s.context_hard_limit_tokens:
        return "hard"
    warm = seconds_since_last_call is not None and seconds_since_last_call <= s.cache_ttl_seconds
    if tokens >= s.context_warm_compact_tokens and warm:
        return "warm"
    return None


async def compact(
    *,
    llm: LLM,
    store: Store,
    conv_id: str,
    settings: Settings,
    system: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    model: str,
) -> bool:
    indexed = store.messages_with_idx(conv_id)
    cut = find_cut(indexed)
    if cut is None:
        return False
    head = [m for idx, m in indexed if idx < cut]
    request = {"role": "user", "content": [{"type": "text", "text": SUMMARY_REQUEST}]}
    # head ends with an assistant message by construction, so appending a user turn is valid
    resp = await llm.create(
        model=model,
        system=system,
        messages=head + [request],
        tools=tools,
        max_tokens=settings.max_tokens,
        tool_choice={"type": "none"} if tools else None,
    )
    # Own conv id: last_usage(conv_id) drives the compaction decision and must keep describing the main agent's call.
    store.usage_add(f"{conv_id}#compact", model, resp.usage)
    summary = "".join(b["text"] for b in resp.content if b["type"] == "text").strip()
    if not summary:
        return False
    store.replace_prefix(
        conv_id,
        cut,
        [{"type": "text", "text": f"<conversation_summary compacted_at=\"{int(time.time())}\">\n{summary}\n</conversation_summary>"}],
    )
    return True
