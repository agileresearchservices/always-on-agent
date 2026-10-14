# aoa: an always-on agent prototype

A working prototype of the architecture in the Stanford CS146S lecture "How always-on agents work":
a model calling tools in a loop, made **persistent** (survives crashes and restarts), **proactive**
(woken by events, knows when to stay quiet) and **long-running** (memory, compaction, helpers).

Python, Anthropic SDK, Temporal. Default model `claude-sonnet-5-5`, reviewer `claude-haiku-5-5`.

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'
export ANTHROPIC_API_KEY=...
export AOA_ALLOW_INSECURE=1               # local dev only; otherwise set AOA_API_TOKEN and AOA_WEBHOOK_SECRET

temporal server start-dev                 # terminal 1 (Temporal CLI)
python -m aoa.worker                      # terminal 2
uvicorn aoa.server:app --port 8000        # terminal 3
python -m aoa.client_cli --session main   # terminal 4
```

Trigger it from outside the chat:

```bash
curl -XPOST localhost:8000/webhooks/email -H 'content-type: application/json' \
  -d '{"text":"Invoice from Acme overdue","dedupe_key":"msg-123"}'
```

Tests: `pytest` runs 47 tests with a scripted fake model and no server.
`pytest -m temporal` runs the end-to-end tests once `temporal server start-dev` is up (they skip otherwise).

## Configuration and secrets

`.env.example` lists every variable. Nothing in aoa reads a `.env` file, so inject secrets through your
host (systemd `EnvironmentFile`, Docker/Kubernetes secrets, a secrets manager) and never commit them.
The API server refuses to start unless `AOA_API_TOKEN` and `AOA_WEBHOOK_SECRET` are set; export
`AOA_ALLOW_INSECURE=1` to bypass that for local development only. For a real deployment you also need a
production Temporal (not `start-dev`) and a persistent, absolute `AOA_DATA_DIR`.

## Running it as services (macOS)

`deploy/install-launchd.sh install` writes four launchd user agents (`com.aoa.temporal`, `worker`, `server`,
`slack-bridge`) that start at login and restart on failure, reading `.env` through `deploy/run.sh`. Logs go to
`<AOA_DATA_DIR>/logs/`. Temporal runs `start-dev` with a file database (`<AOA_DATA_DIR>/temporal.db`) so
workflows and schedules survive restarts; it is still a dev server, not a production cluster. Stop any
hand-started aoa processes first (they hold ports 7233 and 8000). `status` and `uninstall` subcommands exist.

## Slack client

`python -m aoa.slack_bridge` (needs `pip install -e '.[slack]'`) connects a private Slack channel to a
session: your messages go in as trusted user messages, replies and approval requests (with buttons) come
back. Only one allowed user in one channel is honored. Setup: `docs/slack-setup.md`.

## How the lecture maps to the code

| Lecture idea | Where |
|---|---|
| Model + tools in a loop | `loop.py` (no I/O, deterministic, shared by Temporal and the in-process runner) |
| Thin client, thick server; one tool crosses the boundary (`send_user`) | `tools.h_send_user`, `notify.py`, `store.outbox`, `client_cli.py` |
| Durable workflow, not a job queue (resume at step 3, not step 1) | `workflows.SessionWorkflow`; every model and tool call is an activity; `Store.commit_step` makes steps idempotent |
| User message preempts, others queue, bursts batch | `submit_event` queues by priority; steering merges new user events into the in-flight step (`core.append_results`); batch window for background events |
| Triggers: Slack, email, webhooks, routines | `server.py` webhooks (untrusted); routines are Temporal Schedules (`scheduling.py`); all use signal-with-start (`delivery.py`) |
| Sleep/wake the computer; snapshot state | `sandbox.LocalSandbox` (tar snapshot); the workflow sleeps it after `AOA_IDLE_SLEEP` seconds idle |
| Know when to be quiet | `notify.Notifier`: replies/questions/approvals/urgent always go; updates are held in a digest when too soon or in quiet hours |
| Cheapest option first, ask the human last | `prompt.OPERATING_RULES` |
| Files all the way down; profile, dated log, scratchpad, skills | `memory.py` |
| Dynamic tool discovery (names only, schema on demand) | `tools.py`: core vs deferred, `tool_search`, `call_deferred` |
| Subagents with fresh context, results only | `spawn_helper` runs a child workflow (`HelperWorkflow`), several in parallel |
| Prompt caching: static prefix, fixed order, cache miss = bug | `prompt.py` (hashed sections, experiments only append), `llm.place_cache_breakpoints`, miss detection in `core.llm_step`, volatile context lives in the user message |
| Warm the cache while the user types | `POST /sessions/{id}/typing` -> `warm_cache` activity |
| Compact inside the warm window | `compaction.py`: summariser reuses the exact cached prefix |
| Long output goes to a file | `sandbox._spill`, `core._cap` |
| Auto-review of commands by a second model | `safety.py`: denylist, then reviewer model, fail closed |
| Untrusted input is data; irreversible actions need approval | `events.render_event_text`, `safety.wrap_untrusted`, `external_send` approval flow |
| Errors written for agents | `ToolError` messages say what to do next |
| Watch, review, merge (PR flow) | `github_monitor.py` announces PRs; `PrReviewWorkflow` runs a reviewer sub-agent and posts a comment review (`pr_review.py`); an approved card gets a Slack [Merge] button that starts `MergePrWorkflow` (`pr_merge.py`): wait for checks, squash-merge pinned to the reviewed commit, delete the branch, confirm linked issues closed. On conflicts the click instead has `pr_resolve.py` resolve them in a throwaway clone (model per file, validated), run the repo's tests with no secrets in the environment, and push; the new commit is reviewed again and needs a second click. The model never has a merge tool |

## Honest limits

- **The sandbox is not isolation.** `LocalSandbox` runs commands as your user in a directory. It stands in
  for the Firecracker microVM; implement the `Sandbox` interface on a VM or container runtime before
  letting it run anything you do not trust. The denylist and reviewer are guardrails, not a jail.
- **Tools run at most once.** A worker dying mid-command is reported to the model as "effects unknown", never
  silently re-run. Model calls are retried safely.
- **Not implemented:** computer use (screenshots, accessibility tree), fact garbage collection beyond a size cap,
  voice, payments, real Slack/email connectors (`external_send` records instead of sending), eval/flywheel tooling.
- **Single host.** Workers share one SQLite file. For several hosts, move `store.py` to Postgres.
- **Auth is minimal** (two shared secrets, required unless `AOA_ALLOW_INSECURE=1`). Put it behind real auth and TLS before exposing it.
- `warm_cache` reliably warms the system and tool blocks; warming conversation history is best effort.
- Routines inherit trust from whoever created them, so only a user-started turn may create one.
- **The API token is reachable from the sandbox.** Merges and approvals are triggered through the local API with
  `AOA_API_TOKEN`, and a non-isolated sandbox command could read `.env`. The command reviewer stands between the
  model and that; real isolation (above) is the fix.
- **Conflict resolution runs the repo's tests as your user.** After aoa resolves a PR's conflicts it runs the
  repo's test command on this machine (allowlisted env, throwaway HOME, clone outside the data dir), which is
  the repo's own code, as if you ran it. Only your own reviewed PRs get there, and the command comes from config
  or the base branch, never the PR. Running it in a container is #21; until then, do not use this on code you do not trust.
