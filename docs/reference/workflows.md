# Workflows and Temporal

Source: `aoa/workflows.py` (workflows), `aoa/activities.py` (activities), `aoa/worker.py`, `aoa/delivery.py`,
`aoa/scheduling.py`, `aoa/search_attrs.py`, `tests/test_replay.py`, `tests/histories/`.

Temporal gives the agent durable execution: a turn survives a crash of the worker, a timer survives a restart, and a
session can live for days. The price is a strict rule about what workflow code may do.

## The workflows

| Workflow | Started by | Does |
|---|---|---|
| `SessionWorkflow` | `delivery.py` signal-with-start (id `session-<name>`) | One per session, long-lived. Two queues (user, background), a batch window, the model loop, cache warming, idle to sandbox sleep, digest flush timer, approval nudge and expiry timers, continue-as-new |
| `HelperWorkflow` | `spawn_helper` (child) | A fresh conversation for a delegated task. PR reviews run as helpers |
| `RoutineWorkflow` | A Temporal Schedule (cron) | Hands the routine's prompt to the session as an event |
| `GithubPollWorkflow` | Schedule `aoa-monitor-github` (every `AOA_GITHUB_POLL_SECONDS`) | Runs `poll_github`, starts one `PrReviewWorkflow` per PR to review (abandoned children, so a slow review never blocks the next poll) |
| `PrReviewWorkflow` | `GithubPollWorkflow` | One review of one PR at one commit: fetch the diff, run the reviewer helper, publish |
| `MergePrWorkflow` | A Slack **Merge** click via the server | Merge the reviewed commit; on conflict resolve in a temp clone and push; report |
| `EvalProposalWorkflow` | `python -m aoa propose` | Draft an eval case from a flag, validate it, post a card |
| `EvalRunWorkflow` | Schedule `aoa-eval-run` (off by default) | One never-retried activity: the scheduled behaviour evals |
| `FlagScanWorkflow` | Schedule `aoa-flag-scan` (daily) | Scan stored traces for flags; post one digest |
| `FixPrWorkflow` | A Slack **Draft fix** click via the server | Draft a fix locally, wait for a human click, verify, push, open a PR |

`SessionWorkflow` exposes signals `submit_event` (dedupes on `dedupe_key`, keeping the last 500), `typing` (asks for a
cache warm-up), and `stop`, and a query `status`.

## Determinism: what workflow code must not do

A workflow is replayed from its history, so the same code must issue the same commands in the same order.

- No clocks (use `workflow.now()`), no randomness, no network or file I/O, no reading `Settings` or environment.
  Configuration arrives as the workflow input (`cfg` dict).
- `loop.py` runs inside the workflow, so it is held to the same rule. It talks to the world only through `Ops`.
- Anything non-deterministic belongs in an **activity**.

## Replay compatibility

`SessionWorkflow` runs for days and PR workflows run across deploys, so a code change can meet a history recorded by
older code. **Any change to the commands a workflow issues** (a new activity, timer, child start, or a reordering) needs a
guard:

```python
if workflow.patched("name-of-change"):
    ...new behaviour...
```

Existing guards in `workflows.py`: `digest-flush`, `approval-expiry`, `pr-search-attrs`, `inline-diff`,
`resolve-conflicts`. New workflows (`EvalProposalWorkflow`, `EvalRunWorkflow`, `FlagScanWorkflow`, `FixPrWorkflow`) need
no guard because no history of them predates the code.

`tests/test_replay.py` replays every JSON in `tests/histories/`, including two recorded by an older worker
(`legacy_*`), and fails if a change would break one. **Never re-record a history to make a failure pass**: the failure
is the finding. To cover a new workflow path, add a scenario to `tests/histories/capture.py` and run it against a local
Temporal server.

An activity's result shape is part of the same contract: changing it must stay replay-compatible with in-flight
workflows (see how `GithubPollWorkflow` still accepts the old integer result).

## Activities

`activities.py` wraps `AgentCore` methods one to one (`prepare_turn`, `llm_step`, `run_tool`, `append_results`,
`maybe_compact`, `warm_cache`) and adds the PR, merge and flywheel activities. Rules:

- **Tools run at most once** (`NO_RETRY` in `WorkflowOps.run_tool`). Model calls are retried.
- Activities that write state do it through `Store.commit_step` keyed by a step key, so a retry returns the stored result.
- `run_scheduled_evals` is never retried because it costs tokens.

## Schedules

| Schedule | Interval | Off switch | Created by |
|---|---|---|---|
| `aoa-monitor-github` | `AOA_GITHUB_POLL_SECONDS` (600) | not created when `GITHUB_TOKEN` or `AOA_GITHUB_USER` is unset | `scheduling.ensure_github_schedule` |
| `aoa-flag-scan` | `AOA_FLAG_SCAN_HOURS` (24) | `0` | `flag_schedule.ensure_flag_schedule` |
| `aoa-eval-run` | `AOA_EVAL_SCHEDULE_HOURS` (0 = off) | `0` deletes it | `eval_schedule.ensure_eval_schedule` |
| one per routine | the routine's cron (UTC) | `delete_routine` | `scheduling.py` |

The worker ensures them at start. To fire one by hand: `temporal schedule trigger <id>`.

## Search attributes

PR workflows carry `Repo`, `PrNumber`, `Sha` and `Session`, registered by `search_attrs.ensure_registered` at worker and
server start. Find a PR's workflows with:

```bash
temporal workflow list --query 'PrNumber=27'
```

A child start that names an unregistered attribute fails the workflow *task* (it cannot be caught in workflow code),
which would stop every later poll. That is why the poll only passes them when the worker confirmed the server knows them.

## Testing workflows

- Prefer `direct.handle_events` with `FakeLLM`: no server needed.
- `@pytest.mark.temporal` tests (`tests/test_temporal_e2e.py`) need `temporal server start-dev`; they use throwaway
  `aoa-test-*` task queues. **Every workflow a test starts must end**, or it stays Running forever. After a run,
  `temporal workflow list --query 'ExecutionStatus="Running"'` should list only `session-main`.
