"""Flag bad outcomes from the traces already in SQLite (flywheel step 1, #54).

`scan` reads the `steps` and `approvals` tables and records one `flagged` row per (conversation, reason). It is
idempotent, so it can run any time (`python -m aoa flags --scan`). A flag is a lead for a human or a later
workflow, not a verdict: nothing here changes behaviour, sends anything or costs a model call.

`render_trace` is also the stable export format: a plain, ordered view of a conversation that does not depend on
how the agent works inside.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any

from .config import Settings
from .store import Store

TOOL_ERROR_REPEATS = 3  # the same tool failing this many times in one conversation is worth a look
REPORTED_OPEN_CAP = 20  # open model-reported issues per conversation, so a bad turn cannot flood the list
EVIDENCE_CHARS = 500

REPORT_KINDS = ("wrong_answer", "bad_tool_use", "missed_rule", "too_noisy", "other")


def _clip(text: str, n: int = EVIDENCE_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _turn_of(step_key: str) -> str | None:
    """`llm:<turn>.s<N>` -> `<turn>`. Helper turns and side calls use other keys and are skipped by callers."""
    if not step_key.startswith("llm:") or ".s" not in step_key:
        return None
    return step_key[4:].rsplit(".s", 1)[0]


def scan(store: Store, settings: Settings) -> list[dict[str, Any]]:
    """Record newly seen bad outcomes; return the rows added by this scan."""
    added: list[tuple[str, str, str | None, str]] = []

    for ap in store.approvals_denied():
        a = ap["action"]
        what = a.get("tool") or a.get("action") or "action"
        added.append((ap["session"], f"approval_denied:{ap['id']}", None, _clip(f"the person denied {what}: {json.dumps(a)}")))

    for conv in store.conv_ids():
        steps = store.steps_for(conv)
        tool_names: dict[str, tuple[str, str]] = {}  # tool_use id -> (name, turn), from the model's own calls
        misses: list[str] = []
        turns: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        for key, res in steps:
            if not key.startswith("llm:"):
                continue
            turn = _turn_of(key)
            for tu in res.get("tool_uses", []):
                tool_names[tu["id"]] = (tu["name"], turn or "?")
            if turn is not None:
                turns[turn].append((key, res))
            if res.get("stop_reason") == "max_tokens":
                added.append((conv, f"max_tokens:{key}", key, "the model's output was cut off at max_tokens"))
            if res.get("cache_miss"):
                misses.append(key)
        if misses:  # one flag per conversation: a restart that changes the tool index makes one expected miss
            added.append((conv, "cache_miss", misses[0],
                          f"prompt cache missed within the TTL {len(misses)} time(s), first at {misses[0]} (a stability bug if not a restart)"))
        for turn, llm_steps in turns.items():
            last_key, last = llm_steps[-1]
            if len(llm_steps) >= settings.max_steps_per_turn and last.get("tool_uses"):
                added.append((conv, f"max_steps:{turn}", last_key,
                              f"the turn used all {settings.max_steps_per_turn} steps and was still calling tools"))
        errors: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)  # (tool, turn) -> failures
        for key, res in steps:
            if key.startswith("tool:") and res.get("is_error"):
                errors[tool_names.get(res.get("tool_use_id", ""), ("unknown", "?"))].append((key, str(res.get("content", ""))))
        for (name, turn), errs in errors.items():  # per turn, so scattered failures over months do not add up
            if len(errs) >= TOOL_ERROR_REPEATS:
                added.append((conv, f"tool_errors:{name}:{turn}", errs[0][0],
                              _clip(f"{name} failed {len(errs)} times in one turn; first: {errs[0][1]}")))

    return [{"conv_id": c, "reason": r, "step_key": k, "evidence": e}
            for c, r, k, e in added if store.flag_add(c, r, e, k)]


def report(store: Store, conv: str, kind: str, summary: str) -> str:
    """The model's own report (the `report_issue` tool). A hint, so it is labelled as such and capped."""
    if kind not in REPORT_KINDS:
        raise ValueError(f"kind must be one of: {', '.join(REPORT_KINDS)}")
    if store.flags_count(conv, "reported:", "open") >= REPORTED_OPEN_CAP:
        return "not recorded: too many open reports from this conversation"
    digest = hashlib.sha256(summary.encode()).hexdigest()[:8]
    fresh = store.flag_add(conv, f"reported:{kind}:{digest}", _clip(f"[model-reported, unverified] {summary}"))
    return "recorded" if fresh else "already recorded"


def _block_text(block: dict[str, Any]) -> str:
    t = block.get("type")
    if t == "text":
        return block["text"]
    if t == "tool_use":
        return f"[tool_use {block['name']}] {json.dumps(block.get('input', {}), ensure_ascii=False)}"
    if t == "tool_result":
        c = block.get("content")
        c = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
        return f"[tool_result{' ERROR' if block.get('is_error') else ''}] {c}"
    return f"[{t}]"  # thinking and anything else: left out of the text form (the JSON form keeps every block as stored)


def render_trace(store: Store, conv: str, *, as_json: bool = False, max_chars: int = 2000) -> str:
    """A conversation as ordered messages. `max_chars` clips each block in the text form (0 = no clip)."""
    msgs = [{"idx": i, **m} for i, m in store.messages_with_idx(conv)]
    if as_json:
        return json.dumps({"conv": conv, "messages": msgs}, indent=1, ensure_ascii=False)
    if not msgs:
        return f"no messages for {conv!r}"
    out = [f"conversation {conv}"]
    for m in msgs:
        for b in m["content"]:
            text = _block_text(b)
            if max_chars and len(text) > max_chars:
                text = text[:max_chars] + f"… [{len(text)} chars]"
            out.append(f"#{m['idx']} {m['role']}: {text}")
    return "\n".join(out)


def format_flags(flags: list[dict[str, Any]]) -> str:
    if not flags:
        return "no flagged outcomes"
    return "\n".join(f"[{f['status']}] {f['conv_id']}  {f['reason']}\n    {f['evidence']}" for f in flags)
