# Store: the SQLite schema

One SQLite file, `<AOA_DATA_DIR>/aoa.sqlite3`, in WAL mode, shared by the worker, the server and the CLI commands.
Source: `aoa/store.py` (`SCHEMA` and class `Store`). Temporal history holds orchestration only; **all conversation data
lives here**. Every connection waits up to 30 s on a lock, and multi-row writes use `BEGIN IMMEDIATE`.

Design rule: a state change and its idempotency record are written in **one transaction**, so a retried activity cannot
duplicate a message or a charge.

## Tables

| Table | Key | Holds |
|---|---|---|
| `messages` | `(conv_id, idx)` | The conversation: `role` (`user` / `assistant`) and `content`, a JSON list of Anthropic content blocks. `idx` grows by one per message. Compaction replaces a prefix (`replace_prefix`) |
| `steps` | `(conv_id, step_key)` | Idempotency records: the JSON result of each durable step. `commit_step` writes the message **and** this row atomically; if the row exists it returns the stored result with `fresh=False` and writes nothing |
| `events` | `dedupe_key` | Event ids seen, per session. `add_event_once` returns False for a repeat, so a redelivered webhook is dropped |
| `outbox` | `seq` (autoincrement) | Everything the agent says to a client: `kind`, JSON `payload`, and a unique `client_key` that makes a push idempotent. Clients poll `outbox_since(session, after_seq)` |
| `usage` | `id` | One row per model call: `conv_id`, `model`, input / output / cache-read / cache-write tokens. Source of `python -m aoa usage` |
| `approvals` | `id` | Pending human decisions: `action` (JSON), `status` in `pending`, `approved`, `denied`, `expired`. `approval_resolve` and `approval_expire` only act on `pending`, so a decision happens once |
| `routines` | `(session, name)` | Recurring prompts: `cron` (UTC) and `prompt`. Mirrors the Temporal Schedules |
| `kv` | `(session, key)` | Small JSON values. See the key list below |
| `flagged` | `(conv_id, reason)` | Bad outcomes found by `flag.scan`, with `evidence`, `step_key` and `status` (`open`, `declined`, ...). One row per conversation and reason, so scanning is idempotent |
| `eval_proposals` | `id`; unique `(conv_id, reason)` | One proposed eval case per flag: `status`, `spec` (JSON), `info` (JSON, model notes and validation numbers) |

`outbox.kind` values written by the code: `reply`, `update`, `alert`, `question`, `approval`, `urgent`, `reaction` (the
set `notify.KINDS`), plus `eval_proposal`, `sent_log` and `system`. Held updates are delivered later by a durable timer.

`eval_proposals.status` values seen in use: `drafting`, `proposed`, `accepted`, `rejected`, `declined` (the model
wrote no case), `invalid`, `not_reproduced`, `duplicate`, `fix_opened`. The code in `aoa/eval_proposal.py` and
`aoa/fix_pr.py` is the authority.

## `kv` keys

Keys are strings, values are JSON. A key is scoped by its session; the flywheel's daily counters use the session name
`flywheel`.

| Key (prefix) | Written by | Meaning |
|---|---|---|
| `turn_trigger` | `core` | Whether the current turn was started by the user or in the background. Tools read it to apply per-trigger limits |
| `fp:<conv>` | `core.llm_step` | Fingerprint of the last prompt prefix, so a cache miss after a restart can be explained |
| `apr_nudged:<id>` | `core` | An approval has had its one reminder |
| `digest`, `last_notified_ts` | `notify` | Held updates and the time of the last delivery |
| `github_seen`, `github_review_queue` | `github_monitor` | Which head commit of each PR was already reported; the review queue |
| `prreview-posted:<repo>#<n>@<sha>` | `activities` | A review was already posted for that head commit |
| `merge-offer:<id>`, `merge-offer-for:...` | `activities`, `pr_merge` | The opaque offer behind a Merge button, and the offer made for a given head commit |
| `aoa-resolved:<repo>#<n>@<sha>` | `activities` | Files aoa resolved in that commit's conflict resolution |
| `eval_runs`, `eval_baseline` | `eval_schedule` | Last 30 scheduled eval summaries; the last green summary |
| `eval_last_posted`, `eval_last_error`, `eval_error_at` | `eval_schedule` | Dedupe for regression and failure alerts |
| `fixcount:<UTC date>` under session `flywheel` | `server` | `[Draft fix]` clicks that day (atomic claim with `kv_update`) |

`Store.kv_update(session, key, fn)` is read-modify-write in one transaction; use it for counters.

## Rules for changing the schema

- Add tables with `CREATE TABLE IF NOT EXISTS` in `SCHEMA`; it runs on every start. There is no migration tool and no
  `ALTER` anywhere in the code today. A new column on an existing table would need a guarded `ALTER`, because
  `IF NOT EXISTS` skips a table that exists; none has been needed yet.
- Keep every state change inside one `_tx` together with its idempotency record.
- A new activity result shape must stay replay-compatible with in-flight workflows.
- Never put secrets in `payload`, `kv` or `info`: they reach Slack and the CLI.

## Useful queries

```bash
sqlite3 data/aoa.sqlite3 "select seq,kind,substr(payload,1,120) from outbox order by seq desc limit 5"
sqlite3 data/aoa.sqlite3 "select conv_id,reason,status from flagged"
sqlite3 data/aoa.sqlite3 "select id,action,status from approvals where status='pending'"
python -m aoa trace <conv_id>      # a conversation, readable
```
