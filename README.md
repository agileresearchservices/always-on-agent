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
python -m aoa usage                       # estimated API spend per day (from the usage table)
python -m aoa flags --scan                # (also run daily by the aoa-flag-scan schedule, AOA_FLAG_SCAN_HOURS; it posts one Slack digest, never proposes) list flagged bad outcomes (denied approvals, cut-off or capped turns, repeated tool errors, cache misses, report_issue)
python -m aoa trace <conv> [--json]       # a conversation's messages, the stable trace export
python -m aoa evalruns                   # scheduled eval runs and whether each regressed (AOA_EVAL_SCHEDULE_HOURS; off by default)
python -m aoa propose [--limit N]         # draft + validate eval cases for open flags (model calls and eval runs, capped per day); cards arrive in Slack
python -m aoa proposals                   # list eval-case proposals and their status
```

Trigger it from outside the chat:

```bash
curl -XPOST localhost:8000/webhooks/email -H 'content-type: application/json' \
  -d '{"text":"Invoice from Acme overdue","dedupe_key":"msg-123"}'
```

Tests: `pytest` runs the whole fast suite (400+ tests) with a scripted fake model and no server.
`pytest -m temporal` runs the end-to-end tests once `temporal server start-dev` is up (they skip otherwise).

## Documentation

| Read this | For |
|---|---|
| [docs/architecture.md](docs/architecture.md) | The system picture, one message end to end, a module map of every file |
| [docs/reference/](docs/reference/) | One page per subsystem: [store](docs/reference/store.md), [tools](docs/reference/tools.md), [workflows](docs/reference/workflows.md), [trust and safety](docs/reference/trust-and-safety.md), [prompt and cache](docs/reference/prompt-and-cache.md), [GitHub pipeline](docs/reference/github-pipeline.md), [flywheel and evals](docs/reference/flywheel.md), [configuration](docs/reference/configuration.md) |
| [docs/build-guide.md](docs/build-guide.md) | Rebuilding the project in stages, each with a check |
| [docs/operations.md](docs/operations.md) | Running it as services, restarting, cost, a runbook |
| [docs/decisions.md](docs/decisions.md) | Why it is built this way, and what was deliberately left out |
| [docs/slack-setup.md](docs/slack-setup.md) | Creating the Slack app |

A test (`tests/test_docs_in_sync.py`) fails if the configuration, tool or store pages miss something the code defines, or a
doc link breaks.

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
| Know when to be quiet | `notify.Notifier`: replies/questions/approvals/urgent always go; updates are held in a digest when too soon or in quiet hours, and a durable timer in `SessionWorkflow` (`digest_status` / `flush_digest` activities) delivers the digest when the spacing or quiet hours allow, even if nothing else happens |
| Cheapest option first, ask the human last | `prompt.OPERATING_RULES` |
| Files all the way down; profile, dated log, scratchpad, skills | `memory.py` |
| Dynamic tool discovery (names only, schema on demand) | `tools.py`: core vs deferred, `tool_search`, `call_deferred` |
| Subagents with fresh context, results only | `spawn_helper` runs a child workflow (`HelperWorkflow`), several in parallel |
| Prompt caching: static prefix, fixed order, cache miss = bug | `prompt.py` (hashed sections, experiments only append), `llm.place_cache_breakpoints` (5m or 1h via `AOA_CACHE_TTL`), miss detection in `core.llm_step`, volatile context lives in the user message |
| Warm the cache while the user types | `POST /sessions/{id}/typing` -> `warm_cache` activity |
| Compact inside the warm window | `compaction.py`: summariser reuses the exact cached prefix |
| Long output goes to a file | `sandbox._spill`, `core._cap` |
| Auto-review of commands by a second model | `safety.py`: denylist, then reviewer model (answers constrained to a JSON schema via structured outputs), fail closed |
| Untrusted input is data; irreversible actions need approval | `events.render_event_text`, `safety.wrap_untrusted`, `external_send` approval flow; a pending approval is nudged once after `AOA_APPROVAL_NUDGE` (4h) and lapses after `AOA_APPROVAL_EXPIRE` (24h) via workflow timers |
| Errors written for agents | `ToolError` messages say what to do next |
| Ask about GitHub from Slack | `tools.py`: `github_list` (open/closed PRs or issues, one repo or all yours), `github_read` (a PR with diff, an issue, comments, CI checks): read-only, untrusted-wrapped, your repos only; `github_write` (issue, comment, review, close) still needs an approval card |
| Watch, review, merge (PR flow) | `github_monitor.py` announces PRs; `PrReviewWorkflow` runs a reviewer sub-agent and posts its review (`pr_review.py`; findings are checked against the PR's diff before posting: a file or line the diff does not show is removed from a finding, which is kept, and `<data_dir>/review-focus/<owner>__<repo>.md` adds your standing notes for a repo): a real APPROVE / REQUEST_CHANGES when a reviewer identity is configured (`gh_reviewer.py`, a GitHub App with per-repo tokens), else a comment review with a stated recommendation; an approved card gets a Slack [Merge] button that starts `MergePrWorkflow` (`pr_merge.py`): wait for checks, squash-merge pinned to the reviewed commit, delete the branch, confirm linked issues closed. On conflicts the click instead has `pr_resolve.py` resolve them in a throwaway clone (model per file, validated), run the repo's tests with no secrets in the environment, and push; the new commit is reviewed again and needs a second click. The model never has a merge tool |

## Honest limits

- **The sandbox is not isolation.** `LocalSandbox` runs commands as your user in a directory. It stands in
  for the Firecracker microVM; implement the `Sandbox` interface on a VM or container runtime before
  letting it run anything you do not trust. The denylist and reviewer are guardrails, not a jail.
- **Tools run at most once.** A worker dying mid-command is reported to the model as "effects unknown", never
  silently re-run. Model calls are retried safely.
- **Not implemented:** computer use (screenshots, accessibility tree), fact garbage collection beyond a size cap,
  voice, payments, email, an `external_send` that actually sends (it records instead; the Slack bridge is only the
  user-facing client), and the eval flywheel (Langfuse capture, evals generated from traces, #1). The seed eval
  suite and batch mode exist (`python -m evals`).
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
  or the base branch, never the PR. Container isolation was considered (#21) and deliberately skipped; do not use this on code you do not trust.
- **The reviewer App's private key is a file your user can read**, like `.env`, so a non-isolated sandbox command
  could read it too. The installation tokens minted from it are scoped to one repo and `pull_requests: write`,
  but the key itself can mint tokens for every repo the App is installed on.
