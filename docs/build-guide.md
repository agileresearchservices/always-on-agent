# Build guide: reproducing aoa from nothing

This guide builds the project in stages. Each stage says what to build, the contract it must meet, and a **check** you
can run to know it works before moving on. The repository is the worked answer: for any stage, read the named files and
run the named tests. The tests are the specification.

Read [architecture.md](architecture.md) first. Allow a few days for all stages; stages 1 to 6 give a working agent without
any server, and stage 7 is where it becomes durable.

## Stage 0: prerequisites

- Python 3.11 or newer.
- An Anthropic API key (`ANTHROPIC_API_KEY`). Stages 1 to 6 and the whole default test suite need none.
- The [Temporal CLI](https://docs.temporal.io/cli) for stage 7 (`temporal server start-dev` is a local server with a
  SQLite file; no Docker needed).
- Optional: a Slack app (stage 9, see [slack-setup.md](slack-setup.md)), a GitHub token (stage 10), the Claude Code CLI
  (stage 11's fix PRs).

```bash
python -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'     # add ,slack ,github-app as needed
pytest -q                                                                    # the reference suite, no server, no key
```

Dependencies are in `pyproject.toml`: `anthropic`, `temporalio`, `fastapi`, `uvicorn`, `httpx`; optional `slack_sdk` +
`aiohttp`, and `pyjwt[crypto]` for a GitHub App identity.

## Stage 1: configuration and the store

Build `config.py` (`Settings.from_env`: every knob an `AOA_*` env var, a bad value warns and falls back, never crashes)
and `store.py` (SQLite, WAL, the schema in [reference/store.md](reference/store.md)).

**Contract.** `commit_step` writes a message and its idempotency record in one transaction and returns the stored result
if the step already exists. `outbox_push` with a repeated `client_key` is a no-op. `approval_resolve` decides once.

**Check.** `pytest tests/test_config_env.py tests/test_notify_atomic.py` and
`tests/test_loop_direct.py::test_llm_step_is_idempotent_on_replay` (a replayed step does not duplicate a message).

## Stage 2: events and the model interface

`events.py` (`Event`, priorities, `render_event_text` that fences untrusted text), `llm.py` (a small `LLM` interface, the
Anthropic implementation, `place_cache_breakpoints`), `testing.py` (`FakeLLM` that replays a script of content blocks).

**Contract.** An LLM call takes system blocks, messages and tools and returns content blocks plus usage. `FakeLLM`
implements the same interface, so everything above can be tested without a key.

**Check.** The cache-breakpoint tests in `tests/test_units.py` (`test_cache_breakpoints_placed_and_capped`,
`test_a_breakpoint_never_lands_on_a_thinking_block`) and `tests/test_loop_direct.py::test_untrusted_event_is_fenced`.

## Stage 3: prompt, tools, sandbox, safety, memory

- `prompt.py`: fixed-order sections; volatile context goes in the user message ([reference/prompt-and-cache.md](reference/prompt-and-cache.md)).
- `tools.py`: `ToolSpec`, `REGISTRY`, core vs deferred, `dispatch` ([reference/tools.md](reference/tools.md)). Start with
  `send_user`, `run_shell`, `read_file`, `write_file`, `memory_*`, `tool_search`, `call_deferred`.
- `sandbox.py`: a working directory with sleep/wake. `safety.py`: command denylist, reviewer model, `wrap_untrusted`.
- `memory.py`: file-based memory (profile facts, dated log, scratchpad, skills) under `<AOA_DATA_DIR>/memory`.
- `notify.py`: the digest and quiet-hours policy; `send_user` is the only path to the client.

**Contract.** A tool handler is `async (ctx, args) -> str` and raises `ToolError` with an actionable message. External text
is always wrapped. The reviewer fails closed.

**Check.** `pytest tests/test_units.py` (sandbox, memory, notifier, denylist, fail-closed reviewer, URL guard) and the tool
tests in `tests/test_edge.py`.

## Stage 4: the core and the loop

`core.py` (`AgentCore` with `prepare_turn`, `llm_step`, `run_tool`, `append_results`, each idempotent by step key),
`loop.py` (`run_agent_loop` against the `Ops` protocol; **no I/O, no clocks**), `direct.py` (`DirectOps` and
`handle_events`).

**Contract.** The loop is: call the model; if it asked for tools, run them (at most once each), append results, repeat;
stop on a final answer or `AOA_MAX_STEPS`. A user event preempts and is steered in through `append_results`.

**Check.** `pytest tests/test_loop_direct.py`. At this point `direct.handle_events` runs a full scripted conversation with
`FakeLLM`, and with a real key it is a working agent in one process.

## Stage 5: compaction, helpers, cost accounting

`compaction.py` (summarise an old prefix, `Store.replace_prefix`), helper conversations (`spawn_helper`, executed by the
loop), `pricing.py` + `usage_report.py` (`python -m aoa usage`).

**Check.** `test_warm_compaction_replaces_history_with_summary` and `test_helpers_run_in_parallel_and_only_results_return`
in `tests/test_loop_direct.py`; `pytest tests/test_usage_report.py`; `python -m aoa usage` after a real run.

## Stage 6: the HTTP edge

`server.py` (FastAPI: messages and webhooks in, outbox out, approvals; bearer token and webhook secret; refuse to start
without them unless `AOA_ALLOW_INSECURE=1`), `client_cli.py`.

**Check.** `pytest tests/test_edge.py` (messages, webhooks, auth, the long-polled outbox, approvals through the app).

## Stage 7: make it durable with Temporal

`workflows.py` (`SessionWorkflow`, `HelperWorkflow`, `RoutineWorkflow`, `WorkflowOps`), `activities.py` (one thin activity
per core method), `worker.py`, `delivery.py` (signal-with-start), `scheduling.py` (routines as Schedules).

**Contract.** Workflow code is deterministic; activities carry all I/O; tools have no retry, model calls do. See
[reference/workflows.md](reference/workflows.md).

**Check.**
```bash
temporal server start-dev &                      # a local server
pytest -m temporal tests/test_temporal_e2e.py
temporal workflow list --query 'ExecutionStatus="Running"'    # only session-main should remain
```
Then run it for real: `python -m aoa.worker`, `uvicorn aoa.server:app --port 8000`, `python -m aoa.client_cli`.

## Stage 8: lock replay compatibility

Record histories of the workflow paths with `tests/histories/capture.py` (needs the local server) and replay them in
`tests/test_replay.py`. From now on every workflow change needs a `workflow.patched` guard, and a failing replay is a
finding, never something to re-record away.

**Check.** `pytest tests/test_replay.py`.

## Stage 9: Slack

`slack_bridge.py`: a Socket Mode client that only speaks the HTTP API (so the agent has no Slack code). Renders outbox
kinds, buttons for approvals, merges and proposals. Setup: [slack-setup.md](slack-setup.md).

**Check.** `pytest tests/test_slack_bridge.py`, then send the bot a message.

## Stage 10: the GitHub pipeline

In order: `github_monitor.py` (poll, settle window), `pr_review.py` (task, schema, citation check), `gh_reviewer.py`
(reviewer identity), `pr_merge.py`, `pr_resolve.py`, the model tools `github_read/list/write`. See
[reference/github-pipeline.md](reference/github-pipeline.md). Add `GITHUB_TOKEN` and `AOA_GITHUB_USER` to `.env`.

**Check.** `pytest tests/test_github_monitor.py tests/test_pr_review.py tests/test_pr_merge.py tests/test_pr_resolve.py tests/test_gh_reviewer.py`.
Then open a throwaway PR in a scratch repo and watch the review arrive.

## Stage 11: evals and the flywheel

`evals/` first (cases, graders, runner with a fake GitHub), then `flag.py`, `eval_proposal.py`, `eval_schedule.py`,
`fix_pr.py`. See [reference/flywheel.md](reference/flywheel.md).

**Check.**
```bash
pytest tests/test_evals.py tests/test_flag.py tests/test_eval_proposal.py tests/test_eval_schedule.py tests/test_fix_pr.py
export ANTHROPIC_API_KEY=...        # only this
python -m evals --runs 1 --case send_needs_approval      # a real-model check, a few cents
```

## Stage 12: run it as services

`deploy/run.sh` (loads `.env`), `deploy/install-launchd.sh` (four launchd user agents with KeepAlive). See
[operations.md](operations.md).

**Check.** `deploy/install-launchd.sh status` shows four running services; a Slack message gets an answer.

## Definition of done for a rebuild

- `pytest -q` passes (422 tests at the time of writing) with no server and no key.
- `pytest -m temporal` passes against a local server and leaves only `session-main` running.
- `python -m evals --runs 3` has no hard failures.
- `tests/test_docs_in_sync.py` passes, so these docs still match the code.
