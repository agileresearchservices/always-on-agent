# Configuration

Source: `aoa/config.py` (`Settings.from_env`). Defaults and one-line comments for every variable are in
[`.env.example`](../../.env.example); this page groups them by purpose and explains how they are read. A test
(`tests/test_config_env.py`) fails when the code reads an `AOA_*` name that `.env.example` omits, and
`tests/test_docs_in_sync.py` fails when this page omits one.

## How configuration is read

- Everything is an environment variable. **Nothing loads `.env` itself**: export it, or run
  `set -a; . ./.env; set +a`. Under launchd, `deploy/run.sh` loads it for the services.
- `.env` is git-ignored and should be mode 600. Never commit real values.
- Variables prefixed `AOA_` are read in `Settings.from_env`, with a few read where they are used
  (`AOA_SESSION`, `AOA_GITHUB_USER`, `AOA_SERVER`, `AOA_API_TOKEN`, `AOA_WEBHOOK_SECRET`, `AOA_ALLOW_INSECURE`).
- **A bad value never stops startup**, because launchd would crash-loop the services. A non-numeric value logs a warning
  naming the variable (never the value of a `*TOKEN` or `*KEY` one) and uses the default. Empty counts as unset.
  `AOA_CACHE_TTL` and `AOA_GITHUB_POLL_SECONDS` also warn when rounded or floored.
- Workflow code never reads `Settings`. Config reaches a workflow as input (the `cfg` dict), to keep it deterministic.
- Tests redirect all state with `AOA_DATA_DIR` (the `settings` fixture in `tests/conftest.py`).

## Secrets (not `AOA_`-prefixed unless noted)

| Variable | Used by | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | worker, server, eval runner | Required. The eval runner subprocess gets **only** this |
| `AOA_API_TOKEN` | server, clients, Slack bridge | Bearer token for `/sessions/*`. Required unless `AOA_ALLOW_INSECURE=1` |
| `AOA_WEBHOOK_SECRET` | server | Shared secret for `/webhooks/*`, sent as `X-AOA-Secret` |
| `AOA_ALLOW_INSECURE` | server | `1` lets the server start without the two above. Local development only |
| `GITHUB_TOKEN` | worker | The model never receives it. Used for polling, reading, approved writes, and clone/push |
| `SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN` | Slack bridge | Socket Mode app token (`xapp-`) and bot token (`xoxb-`) |
| `AOA_REVIEWER_TOKEN` or `AOA_REVIEWER_APP_ID` + `AOA_REVIEWER_APP_KEY_FILE` | worker | Identity for PR reviews that can APPROVE / REQUEST_CHANGES. Off when unset |
| `AOA_CLAUDE_OAUTH_TOKEN` | fix PRs | From `claude setup-token`. The Claude login the headless fix session uses |

## Where things live

| Variable | Meaning |
|---|---|
| `AOA_DATA_DIR` | SQLite file, memory, sandbox, review-focus notes. Default `./data`; use an absolute path |
| `AOA_TEMPORAL_ADDRESS`, `AOA_TEMPORAL_NAMESPACE`, `AOA_TASK_QUEUE` | Where the Temporal server is, and the task queue the worker serves |
| `AOA_SERVER` | Base URL clients use for the HTTP edge (default `http://127.0.0.1:8000`) |
| `AOA_SESSION` | The session name a client or bridge talks to (default `main`) |
| `AOA_TZ` | Time zone for quiet hours and the volatile-context clock (default `America/New_York`) |

## Models and the cache

| Variable | Meaning |
|---|---|
| `AOA_MODEL`, `AOA_HELPER_MODEL` | Main agent and helper conversations (default `claude-sonnet-5-5`) |
| `AOA_REVIEW_MODEL` | The command reviewer (default `claude-haiku-5-5`) |
| `AOA_CACHE_TTL` | Prompt-cache lifetime: 300 (default) or 3600. Other values are normalised and warned about |
| `AOA_REVIEW_THINKING` | Effort level (`low` to `max`) for adaptive thinking in PR-review conversations only. Off by default |

## Spend bounds (marked `[spend]` in `.env.example`)

| Variable | Default | Bounds |
|---|---|---|
| `AOA_MAX_STEPS` | 40 | Model calls per main-agent turn |
| `AOA_MAX_HELPER_STEPS` | 30 | Model calls per helper conversation (PR reviews are helpers) |
| `AOA_MAX_HELPER_DEPTH` | 1 | How deep helpers may spawn helpers |
| `AOA_MAX_TOKENS` | 16000 | Output tokens per model call |
| `AOA_TOOL_RESULT_MAX_CHARS` | 8000 | Longer tool results spill to a sandbox file |
| `AOA_CONTEXT_HARD_LIMIT` | 120000 | Tokens at which the conversation is compacted |
| `AOA_CONTEXT_WARM_COMPACT` | 50000 | Tokens above which compaction runs while the cache is warm |
| `AOA_EVAL_VALIDATE_RUNS` | 2 | Real-model runs that check a proposed eval case (1 to 5) |
| `AOA_EVAL_PROPOSALS_PER_DAY` | 3 | Proposals drafted per day; 0 = off |
| `AOA_EVAL_SCHEDULE_HOURS` | 0 | Scheduled behaviour evals every N hours; 0 = off |
| `AOA_EVAL_SCHEDULE_RUNS` | 3 | Runs per case in a scheduled run (1 to 5) |
| `AOA_FIX_BUDGET_USD` | 3.0 | Dollar cap for the headless fix session; 0 = fix PRs off |
| `AOA_FIX_PER_DAY` | 3 | `[Draft fix]` clicks per day; 0 = off |
| `AOA_FIX_USE_API_KEY` | 0 | `1` lets the fix session use `ANTHROPIC_API_KEY` when no OAuth token is set |
| `AOA_CLAUDE_BIN` | `claude` | The Claude Code CLI the fix session runs |

## Behaviour and timing

| Variable | Default | Meaning |
|---|---|---|
| `AOA_BATCH_WINDOW` | 2.0 s | Wait for more events before starting a turn |
| `AOA_IDLE_SLEEP` | 600 s | Idle time before the sandbox sleeps (must be positive) |
| `AOA_MIN_UPDATE_INTERVAL` | 1800 s | Minimum gap between non-urgent Slack updates; the rest wait in a digest |
| `AOA_QUIET_START`, `AOA_QUIET_END` | 22, 7 | Quiet hours, in `AOA_TZ` |
| `AOA_SHELL_TIMEOUT` | 60 s | How long a sandbox command may run (must be positive) |
| `AOA_APPROVAL_NUDGE`, `AOA_APPROVAL_EXPIRE` | 4 h, 24 h | One reminder, then expiry (expiry is forced to come after the nudge) |
| `AOA_FLAG_SCAN_HOURS` | 24 | Scan stored traces for bad outcomes every N hours. Free, no model; 0 = off |

## GitHub

| Variable | Default | Meaning |
|---|---|---|
| `AOA_GITHUB_USER` | none | The account whose repos are in scope. Every GitHub tool refuses anything else |
| `AOA_GITHUB_POLL_SECONDS` | 600 | Poll interval, floor 30 |
| `AOA_REVIEW_SETTLE_SECONDS` | 300 | A PR touched this recently is not reviewed yet; 0 = off |
| `AOA_TEST_COMMANDS` | none | JSON map of `owner/repo` to the test command run after a conflict resolution |
| `AOA_RESOLVE_TEST_TIMEOUT` | 1200 s | Timeout for that test command |

`.aoa/test-command` as it is on the **base** branch (never the PR's) is used when `AOA_TEST_COMMANDS` has no entry; else
pytest in a fresh venv for a Python repo with tests.

## Recommended local `.env`

Minimum to run the whole stack for yourself:

```bash
ANTHROPIC_API_KEY=...
AOA_API_TOKEN=$(openssl rand -hex 24)
AOA_WEBHOOK_SECRET=$(openssl rand -hex 24)
GITHUB_TOKEN=...                      # only if you want the GitHub features
AOA_GITHUB_USER=<your account>
SLACK_APP_TOKEN=... SLACK_BOT_TOKEN=... SLACK_CHANNEL_ID=... SLACK_ALLOWED_USER_ID=...
```

Everything else has a working default.
