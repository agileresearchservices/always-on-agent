# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`aoa` is a prototype of an "always-on" agent (Python ≥3.11, Anthropic SDK, Temporal, FastAPI, SQLite). `README.md` has a table mapping each architecture idea to the file that implements it, plus an "Honest limits" section. Read both before changing behavior. Default models: `claude-sonnet-5-5` (main/helper), `claude-haiku-5-5` (command reviewer). There is no linter or formatter configured.

## Commands

```bash
python -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'

pytest                                   # fast suite: scripted FakeLLM, no server, no API key
pytest tests/test_units.py::test_name    # single test (asyncio_mode=auto, so plain `async def` tests work)
pytest -m temporal                       # end-to-end; needs `temporal server start-dev` (skips otherwise)

# full stack (four terminals; needs ANTHROPIC_API_KEY)
temporal server start-dev
python -m aoa.worker
uvicorn aoa.server:app --port 8000
python -m aoa.client_cli --session main
```

All configuration is env vars read in `aoa/config.py` (`Settings.from_env`, all prefixed `AOA_`). Tests redirect state with `AOA_DATA_DIR` (see the `settings` fixture in `tests/conftest.py`).

## Architecture

The key design: **the agent logic is written once as `AgentCore` methods and driven by two interchangeable runners.**

- `core.py` — `AgentCore`: every durable step (`prepare_turn`, `llm_step`, `run_tool`, `append_results`, `maybe_compact`, `warm_cache`, ...) as a plain async method. All state-changing steps are idempotent through `Store.commit_step` / `step_get` keyed by a step key, so a retried activity returns the stored result instead of redoing work.
- `loop.py` — `run_agent_loop`: the model→tools loop. It does **no I/O** (it must stay deterministic because it runs inside a Temporal workflow) and talks to the world only through the `Ops` protocol. Don't add clocks, randomness, or I/O here.
- Two `Ops` implementations: `workflows.WorkflowOps` (each op is a Temporal activity or child workflow; wired to `core` via `activities.py`) and `direct.DirectOps` (plain in-process calls, used by most tests and usable without a server).
- `workflows.py` — `SessionWorkflow` (one long-lived workflow per session: user/background queues, batch window, idle→sandbox sleep, cache warming, continue-as-new), `HelperWorkflow` (child workflow for `spawn_helper`, fresh conversation), `RoutineWorkflow` (fired by Temporal Schedules). Workflow code must stay deterministic; config arrives as workflow input (`cfg` dict), not via `Settings`.
- `store.py` — single SQLite file (WAL) shared by all workers: messages, step idempotency records, event dedupe, client outbox, usage, approvals, routines, kv. Temporal history carries orchestration only; conversation data lives here.
- Event path: `server.py` webhooks (untrusted) / client / routines → `delivery.py` signal-with-start → `SessionWorkflow.submit_event` (priority 0 = user, preempts and is steered into the in-flight step via `append_results`).

Things that are easy to break:

- **Tools run at most once** (`NO_RETRY` in `WorkflowOps.run_tool`); a failed tool is reported to the model as "effects unknown", never re-run. Model calls are retried. Keep new tool handlers consistent with this.
- **Prompt-cache stability** (`prompt.py`, `llm.place_cache_breakpoints`): the system prompt and tool block must be a fixed, ordered prefix; volatile context (time, scratchpad, recent log) goes into the new user message via `volatile_context_block`, not the system prompt. `core.llm_step` logs a warning on a cache miss within the TTL, which is treated as a bug. Compaction and `warm_cache` deliberately reuse the exact main-agent prefix via `_setup("main", 0)`.
- **Tools** (`tools.py`): core tools ship full schemas; deferred tools appear only by name in the prompt and are reached via `tool_search` / `call_deferred`. `spawn_helper` is declared in `tools.py` but executed by the loop, not a handler. Handlers raise `ToolError` with an actionable message for the model; results over `tool_result_max_chars` are spilled to a sandbox file by `core._cap`.
- **Trust**: external event text is rendered/wrapped as untrusted data (`events.render_event_text`, `safety.wrap_untrusted`); commands pass a denylist then a reviewer model that fails closed (`safety.py`); `external_send` goes through the approval flow; only user-started turns (`trigger_user`) may create routines.
- `notify.Notifier` decides whether outbound messages go now or are held in a digest (min interval, quiet hours); replies/questions/approvals/urgent always go.
- `sandbox.LocalSandbox` is not isolation (runs as your user in a directory); `memory.py` is file-based (profile, dated log, scratchpad, skills) under `<data_dir>/memory`.

## Testing

`aoa/testing.py` (shipped in the package) provides `FakeLLM` (scripted responses; helpers `text()` and `tool()`; a step may be content blocks, a callable, or an Exception) and `MemoryScheduler`. `tests/conftest.py`'s `make_core(script)` builds an `AgentCore` with a fixed clock (2026-10-14 14:00 New York) and returns `(core, llm)`; `bg_event` / `usr_event` build event dicts. Prefer testing through `direct.handle_events` rather than needing Temporal; reserve `@pytest.mark.temporal` for workflow-specific behavior (`tests/test_temporal_e2e.py`).
