# Notifications and memory

Source: `aoa/notify.py`, `aoa/memory.py`, `aoa/compaction.py`, `aoa/prompt.py`, `aoa/server.py` (outbox endpoint),
`aoa/slack_bridge.py`, `aoa/client_cli.py`.

## Notifications: "do I bug the person?"

The agent has no direct line to the person. `send_user` calls `Notifier.send`, which decides whether the message goes now
or waits, and writes to the **outbox** table. Clients (Slack bridge, CLI) poll `GET /sessions/{sid}/outbox` and render by
`kind`. Silence is a valid outcome: the prompt tells the agent to skip `send_user` when there is nothing worth saying.

### Kinds

`notify.KINDS` = `reply`, `update`, `alert`, `question`, `approval`, `urgent`, `reaction`. The set `ALWAYS` =
`reply`, `question`, `approval`, `urgent`, `reaction` always goes out immediately.

| Kind | Meaning | Policy |
|---|---|---|
| `reply` | Answering the person directly | Always now |
| `question` | The agent is blocked on their input | Always now |
| `approval` | An approval card | Always now |
| `urgent` | Genuinely time-sensitive | Always now |
| `reaction` | A short acknowledgement instead of a message | Always now; does not count as a notification |
| `alert` | Worth knowing soon, not an emergency (a refused injection, a failing background task) | Skips the spacing between updates, but **waits out quiet hours** |
| `update` | Progress or news | Held when it comes too soon after the last send, or during quiet hours |

Other outbox kinds (`eval_proposal`, `sent_log`, `system`) are written by other code, not by `send`.

### The digest

A held message is appended to kv `digest` (the last 20 are kept). It rides along with the next `update`, `reply` or `alert`
that is allowed out ("Earlier updates: ..."), or is sent alone by a **flush**.

- `Notifier.next_flush_at(session, now)` returns the epoch time the digest may go out: never inside quiet hours and never
  sooner than `AOA_MIN_UPDATE_INTERVAL` after the last send. None means nothing is held.
- `SessionWorkflow` asks the `digest_status` activity after each turn and sets a **durable timer** for that time, then runs
  `flush_digest` (guarded by `workflow.patched("digest-flush")`). The flush is idempotent: its client key names the exact
  digest, so a retry after a crash cannot send it twice.
- `send` runs in one `BEGIN IMMEDIATE` transaction (throttle read, digest read-modify-write, outbox insert, throttle write),
  so two concurrent sends cannot both think "not too soon", and a failure leaves outbox, digest and timestamp untouched.
- A repeated `client_key` returns `duplicate` and sends nothing.

### Quiet hours

`AOA_QUIET_START` (22) to `AOA_QUIET_END` (7) in `AOA_TZ`. The window may wrap midnight (`in_quiet_hours`);
`quiet_end_after` finds the first moment outside it.

### Clients

- The outbox is a long-polled, sequence-numbered log. A client remembers its last `seq` (the Slack bridge keeps it in
  `data/slack_cursor_<session>.txt`) and asks for what is newer.
- Rendering is by kind; unknown kinds fall back to plain text. Cards for approvals, merges and proposals carry buttons whose
  clicks call the server endpoints listed in [architecture.md](../architecture.md).

## Memory

Plain markdown under `<AOA_DATA_DIR>/memory`, so the agent can also grep it with ordinary shell commands. Four layers:

| Layer | Path | Behaviour |
|---|---|---|
| Core facts | `profile.md` | Injected into **every** prompt as the `core_facts` section, so it is capped at 4,000 characters (`CORE_FACTS_MAX_CHARS`). A full file refuses new facts and tells the agent to `facts_rewrite` |
| Dated log | `logs/YYYY-MM-DD.md` | One line per entry with a time. Searched on demand, never injected wholesale; the last 12 lines of today go in the volatile context |
| Scratchpad | `scratchpad.tsv` | Notes with an expiry time (`ttl_minutes`). Injected only while live; expired lines are dropped on read |
| Skills | `skills/<slug>.md` | Procedures the agent wrote down after learning them. Listed in the prompt; the name is slugged to `[a-z0-9_-]` |

Tools: `memory_write` (kinds `fact`, `facts_rewrite`, `log`, scratchpad, skill), `memory_search` (regex, falling back to
plain text; searches every `.md`), `memory_read` (a relative path; refuses anything that escapes the memory directory).

### Why the split

- Core facts must be small and stable, because they are part of the **cached prefix**; changing them invalidates it. See
  [prompt-and-cache.md](prompt-and-cache.md).
- The log and scratchpad change constantly, so they live in the volatile context of the new user message instead.
- Skills are discovered by search, not injected, so they do not cost tokens until needed.

### Compaction

When the conversation passes `AOA_CONTEXT_HARD_LIMIT` tokens (or `AOA_CONTEXT_WARM_COMPACT` while the cache is warm),
`compaction.py` summarises an old prefix and `Store.replace_prefix` swaps it for the summary. Durable knowledge should
already be in memory: the prompt tells the agent to write facts and procedures down, because the conversation itself is
not permanent.
