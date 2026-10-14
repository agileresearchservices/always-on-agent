"""AgentCore: every durable step of the agent as a plain async method.

Temporal activities (activities.py) and the in-process runner (direct.py) are both thin wrappers
over these methods, so the logic is written once and tested without a Temporal server.

Each method that changes state is idempotent via Store.commit_step keyed by a step key.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from . import compaction
from .config import Settings
from .events import render_event_text
from .llm import LLM
from .memory import MemoryStore
from .notify import Notifier
from .prompt import build_sections, helper_sections, system_blocks, volatile_context_block
from .sandbox import LocalSandbox, Sandbox
from .store import Store
from .tools import Scheduler, ToolContext, ToolError, core_tool_definitions, dispatch, tool_index_text

log = logging.getLogger("aoa.core")


MISS_FRACTION = 0.9


def is_cache_miss(prev: dict[str, Any] | None, usage: dict[str, int], ttl: float) -> bool:
    """True if a call inside the cache TTL read back clearly less than the previous call cached.

    Everything the previous call read or wrote is a prefix of this request, so a healthy call reads
    about that many tokens. Catches partial misses (e.g. only system+tools hit), not just read == 0.
    """
    if not prev or (time.time() - prev["ts"]) >= ttl:
        return False
    cached = prev["cache_read"] + prev["cache_write"]
    return cached > 0 and usage.get("cache_read_input_tokens", 0) < MISS_FRACTION * cached


class AgentCore:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        memory: MemoryStore,
        llm: LLM,
        scheduler: Scheduler,
        sandbox_factory: Callable[[str], Sandbox] | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.s = settings
        self.store = store
        self.memory = memory
        self.llm = llm
        self.scheduler = scheduler
        self.notifier = Notifier(store, settings)
        self._sandboxes: dict[str, Sandbox] = {}
        self._sandbox_factory = sandbox_factory or (lambda sid: LocalSandbox(settings.sandbox_dir, sid))
        self._clock = clock

    # ---- helpers -------------------------------------------------------------
    def now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(ZoneInfo(self.s.tz))

    def sandbox(self, session: str) -> Sandbox:
        if session not in self._sandboxes:
            self._sandboxes[session] = self._sandbox_factory(session)
        return self._sandboxes[session]

    def _setup(self, role: str, depth: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
        index = tool_index_text(role)
        sections = build_sections(index, self.memory.core_facts()) if role == "main" else helper_sections(index)
        system = system_blocks(sections)
        tools = core_tool_definitions(role, depth, self.s.max_helper_depth)
        model = self.s.model if role == "main" else self.s.helper_model
        return system, tools, model

    def _ctx(self, session: str, conv: str, role: str, depth: int, trigger_user: bool) -> ToolContext:
        return ToolContext(
            session=session, conv_id=conv, role=role, depth=depth, trigger_user=trigger_user,
            settings=self.s, store=self.store, memory=self.memory, sandbox=self.sandbox(session),
            notifier=self.notifier, llm=self.llm, scheduler=self.scheduler, now=self.now,
        )

    # ---- turn preparation ---------------------------------------------------------
    async def prepare_turn(self, session: str, conv: str, turn_key: str, events: list[dict[str, Any]]) -> dict[str, Any]:
        """Dedupe events, bake volatile context into the new user message, append it (idempotent)."""
        fresh = []
        for ev in events:
            key = ev.get("dedupe_key")
            if key and not self.store.add_event_once(session, key):
                continue
            fresh.append(ev)
        existing = self.store.step_get(conv, f"prep:{turn_key}")
        if existing is not None:
            return existing
        if not fresh:
            return {"added": 0, "dropped": len(events)}
        now = self.now()
        trigger = "user" if any(e.get("trusted") for e in fresh) else fresh[0]["source"]
        blocks: list[dict[str, Any]] = [
            volatile_context_block(
                now, trigger, self.memory.scratch_active(now), self.memory.recent_log(now),
                self.notifier.digest_count(session),
            )
        ]
        for ev in fresh:
            blocks.append({"type": "text", "text": render_event_text(ev)})
            self.memory.append_log(f"event from {ev['source']}: {ev['text'][:90]!r}", now)
        result = {"added": len(fresh), "dropped": len(events) - len(fresh), "trigger_user": trigger == "user"}
        self.store.commit_step(conv, f"prep:{turn_key}", result, role="user", content=blocks)
        return result

    async def prepare_helper(self, conv: str, task: str) -> None:
        blocks = [{"type": "text", "text": f"Task from the main agent:\n\n{task}"}]
        self.store.commit_step(conv, "helper-init", {"ok": True}, role="user", content=blocks)

    # ---- the model step ----------------------------------------------------------------
    async def llm_step(self, session: str, conv: str, step_key: str, role: str, depth: int) -> dict[str, Any]:
        done = self.store.step_get(conv, f"llm:{step_key}")
        if done is not None:
            return done
        system, tools, model = self._setup(role, depth)
        messages = self.store.load_messages(conv)
        prev = self.store.last_usage(conv)
        resp = await self.llm.create(
            model=model, system=system, messages=messages, tools=tools, max_tokens=self.s.max_tokens
        )
        content = resp.content or [{"type": "text", "text": "(no output)"}]
        cache_miss = is_cache_miss(prev, resp.usage, self.s.cache_ttl_seconds)
        if cache_miss:
            log.warning("cache miss in %s within TTL (read %s of ~%s cached tokens): treat as a bug (prefix changed?)",
                    conv, resp.usage.get("cache_read_input_tokens", 0),
                    prev["cache_read"] + prev["cache_write"])
        result = {
            "content": content,
            "stop_reason": resp.stop_reason,
            "usage": resp.usage,
            "tool_uses": [b for b in content if b["type"] == "tool_use"],
            "text": "".join(b["text"] for b in content if b["type"] == "text").strip(),
            "cache_miss": cache_miss,
        }
        stored, fresh = self.store.commit_step(conv, f"llm:{step_key}", result, role="assistant", content=content)
        if fresh:
            self.store.usage_add(conv, model, resp.usage)
        return stored

    # ---- tools -----------------------------------------------------------------------------
    async def run_tool(
        self, session: str, conv: str, role: str, depth: int, tool_use: dict[str, Any], trigger_user: bool
    ) -> dict[str, Any]:
        key = f"tool:{tool_use['id']}"
        done = self.store.step_get(conv, key)
        if done is not None:
            return done
        ctx = self._ctx(session, conv, role, depth, trigger_user)
        is_error = False
        try:
            out = await dispatch(ctx, tool_use["name"], tool_use.get("input") or {})
        except ToolError as e:
            out, is_error = f"Error: {e}", True
        except Exception as e:  # noqa: BLE001
            log.exception("tool %s crashed", tool_use["name"])
            out = (f"Error: {tool_use['name']} crashed ({type(e).__name__}: {e}). Its effects are unknown; "
                   "check state before retrying.")
            is_error = True
        out = await self._cap(ctx, tool_use["id"], out)
        result = {"tool_use_id": tool_use["id"], "content": out, "is_error": is_error}
        self.store.step_put(conv, key, result)
        return result

    async def _cap(self, ctx: ToolContext, tool_use_id: str, text: str) -> str:
        limit = self.s.tool_result_max_chars
        if len(text) <= limit:
            return text
        rel = f".outputs/tool-{tool_use_id}.txt"
        await ctx.sandbox.write_file(rel, text)
        return f"{text[: limit // 2]}\n[... {len(text)} chars total. Full text saved to {rel}; use read_file with offset/limit.]"

    async def append_results(
        self, session: str, conv: str, step_key: str, results: list[dict[str, Any]], events: list[dict[str, Any]]
    ) -> None:
        blocks: list[dict[str, Any]] = [
            {"type": "tool_result", "tool_use_id": r["tool_use_id"], "content": r["content"], "is_error": r["is_error"]}
            for r in results
        ]
        for ev in events:  # steering: fresh events ride with this step's results
            key = ev.get("dedupe_key")
            if key and not self.store.add_event_once(session, key):
                continue
            blocks.append({"type": "text", "text": render_event_text(ev)})
        self.store.commit_step(conv, f"results:{step_key}", {"n": len(blocks)}, role="user", content=blocks)

    # ---- housekeeping --------------------------------------------------------------------------------
    async def maybe_compact(self, session: str, conv: str) -> str:
        last = self.store.last_usage(conv)
        messages = self.store.load_messages(conv)
        if last:
            tokens = last["input_tokens"] + last["cache_read"] + last["cache_write"]
            age = time.time() - last["ts"]
        else:
            tokens, age = compaction.estimate_tokens(messages), None
        mode = compaction.decide(tokens, age, self.s)
        if not mode:
            return "none"
        system, tools, model = self._setup("main", 0)  # same prefix as the main agent => cache hit
        ok = await compaction.compact(
            llm=self.llm, store=self.store, conv_id=conv, settings=self.s, system=system, tools=tools, model=model
        )
        if ok:
            self.memory.append_log(f"compacted conversation ({mode}, ~{tokens} tokens before)", self.now())
        return mode if ok else "none"

    async def sandbox_sleep(self, session: str) -> str:
        sb = self.sandbox(session)
        await sb.sleep()
        return sb.state()

    async def warm_cache(self, session: str, conv: str) -> dict[str, int]:
        """Best effort: write the system+tools cache entries before the person finishes typing."""
        system, tools, model = self._setup("main", 0)
        messages = self.store.load_messages(conv)
        if not messages or messages[-1]["role"] == "assistant":
            messages = messages + [{"role": "user", "content": [{"type": "text", "text": "."}]}]
        resp = await self.llm.create(
            model=model, system=system, messages=messages, tools=tools, max_tokens=1,
            tool_choice={"type": "none"},
        )
        self.store.usage_add(f"{conv}#warm", model, resp.usage)
        return resp.usage
