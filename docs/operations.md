# Operations

How to run aoa day to day on one Mac, and what to do when something is wrong. For configuration see
[reference/configuration.md](reference/configuration.md).

## The four services

Installed by `deploy/install-launchd.sh install` as launchd **user agents**, each started through `deploy/run.sh`, which
loads `.env`, then execs the service. `KeepAlive` is on and `ThrottleInterval` is 10 s, so a crash restarts in about ten
seconds.

| Service | Command | Role | Port / dependency |
|---|---|---|---|
| `com.aoa.temporal` | `temporal server start-dev --db-filename data/temporal.db` | Workflow engine, history in SQLite | 7233 |
| `com.aoa.worker` | `python -m aoa.worker` | Runs workflows and activities, owns schedules | needs Temporal |
| `com.aoa.server` | `uvicorn aoa.server:app` | HTTP edge for clients and webhooks | 8000 on 127.0.0.1 |
| `com.aoa.slack-bridge` | `python -m aoa.slack_bridge` | Slack client | needs the server |

They run **whatever is checked out in the repo**. Changing code on `main` changes what runs at the next restart.

```bash
deploy/install-launchd.sh status                           # state and pid of each
deploy/install-launchd.sh install | uninstall              # (re)write plists and start / stop and remove
launchctl kickstart -k gui/$(id -u)/com.aoa.worker         # restart one (also: server, slack-bridge, temporal)
tail -f data/logs/worker.log                               # logs: data/logs/<service>.log
```

Do not start hand-run copies next to the services: they would fight over ports 7233 and 8000.

## Restarting safely

1. Check what is running: `temporal workflow list --query 'ExecutionStatus="Running"'`. Normally only `session-main`
   (the long-lived session) is. Anything else is in-flight work: a PR review, a merge, a fix draft.
2. Restart the affected service. A worker restart is safe for durable workflows (they resume), but it interrupts any
   activity mid-flight, and a tool activity that dies is reported to the model as "effects unknown".
3. After a worker restart that changed the tool list, expect one prompt-cache miss warning; it is stored with the cause `tools_system` and is not flagged. A miss with any other cause is flagged.
4. Restart the **worker** after changing code in `aoa/` that the worker runs; the **server** for `server.py`; the
   **bridge** for `slack_bridge.py`. Changing `.env` needs a restart of every service that reads the variable.

## Where things are

| Path | What |
|---|---|
| `data/aoa.sqlite3` | All agent data ([reference/store.md](reference/store.md)) |
| `data/temporal.db` | Temporal history |
| `data/memory/` | Profile facts, dated logs, scratchpad, skills |
| `data/sandbox/` | The agent's working directory |
| `data/review-focus/` | Your per-repo review notes (`<owner>__<repo>.md`); created when you add the first one |
| `data/logs/` | One log per service |
| `evals/results/` | Eval run results (git-ignored) |

`data/` and `.env` are git-ignored. **Back up** `data/aoa.sqlite3` with the SQLite online backup, not a file copy, because
of WAL: `sqlite3 data/aoa.sqlite3 ".backup 'aoa-backup.sqlite3'"`.

## Watching cost

```bash
python -m aoa usage --days 7      # estimated spend per day and per source (agent, pr-review, helper, compact, cmd-review, warm, resolve, eval-draft)
```

List prices live in `aoa/pricing.py` (dated; update when prices change). Live services only: eval runs are not in this
table (the eval runner prints its own estimate). Standing spend bounds are the `[spend]` variables in `.env.example`.

## Routine checks

```bash
python -m aoa flags --scan        # new bad outcomes
python -m aoa proposals           # eval-case proposals and their status
python -m aoa evalruns            # scheduled eval history (empty unless the schedule is on)
temporal schedule list            # aoa-monitor-github, aoa-flag-scan (and aoa-eval-run if enabled)
```

## Runbook

**No answer in Slack.**
1. `deploy/install-launchd.sh status`: all four running?
2. `tail -50 data/logs/slack-bridge.log` and `server.log` for a 401 (token mismatch between `AOA_API_TOKEN` in the bridge
   and server) or a Slack disconnect.
3. `temporal workflow list` and `temporal workflow describe --workflow-id session-main`. If it is running, the worker is
   probably busy or the model call failed: `tail data/logs/worker.log`.
4. `sqlite3 data/aoa.sqlite3 "select seq,kind,substr(payload,1,100) from outbox order by seq desc limit 5"`: was a reply
   queued but not shown?

**A client gets 401.** Its `AOA_API_TOKEN` differs from the server's. The CLI prints the fix.

**The agent behaves like it did before a prompt change.** A long conversation keeps its earlier habits in its history
even after a restart. Ask the question again in a fresh state, and confirm the fix with an eval.

**The agent probes PR numbers one at a time.** That is the `github_list` rule in `prompt.py` being ignored; see
[reference/prompt-and-cache.md](reference/prompt-and-cache.md). Check the case `github_list_is_read_only`.

**A review never arrives.** The settle window holds a PR that was touched in the last `AOA_REVIEW_SETTLE_SECONDS`.
`temporal workflow list --query 'PrNumber=<n>'` shows whether a review started; `python -m aoa trace` shows the reviewer's
conversation.

**Merge button did nothing or disappeared.** `pr_merge.RETRYABLE` failures restore it; `needs_human`, `stale` and
`tests_failed` do not. The notice posted in Slack says which.

**`aoa propose` starts nothing.** It says why. "Daily cap reached" gives the count and when the next slot frees (the cap is a
rolling 24 hours); raise `AOA_EVAL_PROPOSALS_PER_DAY` and restart the worker to go sooner. "Nothing to propose: no open flag
without a proposal" means every open flag already has a row (`python -m aoa proposals`). "Every candidate flag already has a
running proposal" means workflows are still running: check `temporal workflow list`.

**Scheduled evals alert.** The alert names the case. Read `evals/results/*.json` (`findings`, `failures`), re-run
`python -m evals --case NAME --runs 3`, and decide whether the agent or the case is wrong. A "no case passed" alert is a
harness or key problem, not a regression.

**A Temporal test left workflows running.** `temporal workflow terminate --workflow-id <id>`; then fix the test so the
workflow ends.

**A service crash-loops.** `tail data/logs/<service>.log`. The usual cause is a missing dependency in `.venv` or a missing
`.env`. Config errors do not cause it: bad values warn and fall back.

## Secrets

- `.env` is mode 600 and git-ignored. Only the environment of each service has the values.
- The eval runner and the fix-verification run get a minimal environment. The model's shell gets only `PATH`, `HOME` (set
  to its working directory) and `LANG`, so it never sees a key or token.
- Never paste a token into Slack, an issue or a commit. If one is exposed, rotate it at the provider.
- The GitHub token needs only the scopes you use. The reviewer identity is separate (`AOA_REVIEWER_*`).

## Upgrading

1. `git pull` on `main` (the services run the checkout).
2. `pip install -e '.[dev]'` if `pyproject.toml` changed.
3. `pytest -q`. For workflow changes also `pytest tests/test_replay.py`.
4. Restart the worker, then the server and bridge if they changed.
5. `temporal workflow list --query 'ExecutionStatus="Running"'` to confirm nothing stuck.
