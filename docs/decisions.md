# Design decisions

Why the project is shaped the way it is. Each entry is a decision, the reason, and what it costs. Read these before
proposing to undo one.

## 1. One agent core, two runners
`AgentCore` holds all logic; Temporal workflows and an in-process runner both drive it. **Why:** the same behaviour is
testable in milliseconds with a scripted model, and durable in production. **Cost:** `loop.py` and workflow code must stay
deterministic, and the `Ops` protocol is a seam to maintain.

## 2. Temporal for durability
**Why:** a session lives for days, survives crashes, and needs timers (digest flush, approval expiry, cache warm-up) and
schedules without a hand-built job system. **Cost:** replay compatibility is a permanent constraint (`workflow.patched`,
recorded histories). Mitigated by `tests/test_replay.py`.

## 3. SQLite for all data, Temporal for orchestration only
**Why:** one inspectable file, transactional writes that tie a state change to its idempotency record, and Temporal history
stays small. **Cost:** a single machine, no migrations tool. Fine for a personal agent; the interface in `store.py` is the
seam if that changes.

## 4. Idempotent steps, tools run at most once
Model calls are retried; tools are not. **Why:** retrying a model call is free of side effects, retrying "send an email" is
not. A failed tool is reported as "effects unknown". **Cost:** a tool crash mid-effect needs a human or a careful model.

## 5. The prompt prefix is sacred
Fixed section order, volatile context in the user message, deferred tools by name only. **Why:** prompt caching makes the
difference between a usable and an expensive agent, and one stray timestamp in the system prompt silently breaks it.
**Cost:** a rule contributors must learn; a cache-miss warning acts as the safety net.

## 6. Deferred tools behind `tool_search`
**Why:** a small, fixed tool block keeps the cached prefix small. Native server-side tool search was evaluated and not
adopted (#38): about 730 deferred-tool tokens is far below the scale it is meant for. **Cost:** one extra call to use a
rare tool.

## 7. External text is data
Untrusted events and tool output are wrapped and labelled; instructions from them are never followed, only surfaced. **Why:**
the agent reads email, PRs and web pages that anyone can write. **Cost:** the agent sometimes asks permission for things
it could have done.

## 8. Merging is never a model tool
Only a human click starts `MergePrWorkflow`. **Why:** the highest-impact action on a repo should not be reachable by any
text the model reads. **Cost:** a click per merge. The click pins the reviewed commit, so what you approved is what merges.

## 9. A separate reviewer identity
**Why:** GitHub does not let an account approve its own PR; a second identity can post a real APPROVE / REQUEST_CHANGES.
Off when unset, with a comment-review fallback. **Cost:** a GitHub App to set up.

## 10. Deterministic graders, no LLM judge
**Why:** a model judging a model hides regressions behind its own variance, and costs more. Graders are plain Python over
the run's outbox and effects. **Cost:** cases must be written to be checkable; soft cases are noisy at small run counts,
which is why they are watched as rates.

## 11. The flywheel is human-clicked at every step
Flags are free and automatic; proposals, accepts, fix drafts and PRs are clicks. **Why:** an agent that edits its own tests
and code unattended is an agent that can learn to pass its tests. The static refusals in `fix_pr.py` (no touching
`evals/`, histories, conftest, `pyproject.toml`) back this up. **Cost:** slower, and the fix numbers are not held-out.

## 12. No sandbox, honestly
`LocalSandbox` runs as your user. Container isolation (#21) was evaluated and skipped; a macOS `sandbox-exec` profile was
tried and dropped because a hostile test can still reach launchd. **Why:** a half-isolation invites trust it has not earned.
The documented control is that no fix code runs before a person has read it, and the resolver is for your own PRs. **Cost:**
this is not safe to point at other people's repositories.

## 13. Five-minute cache lifetime
**Why:** measured on real usage, 97.6% of calls follow another within five minutes, so the one-hour lifetime would cost
about 16% more. **Cost:** a call after a long pause pays a full write. Revisit if usage spacing changes.

## 14. Config only from environment variables, never crash on a bad one
**Why:** launchd restarts a crashing service every ten seconds, so a typo in `.env` must not become a crash loop. A bad
value warns (naming the variable, never a secret's value) and uses the default.

## 15. Scheduled evals are opt-in
**Why:** they spend real money (about $0.35 a run). They are off unless `AOA_EVAL_SCHEDULE_HOURS` > 0, skip when nothing
that shapes behaviour changed, and a manual run is always available.

## Things deliberately not built

Langfuse / OpenTelemetry, an LLM judge, a held-out and a frontier eval set, thumbs-down feedback signals, container
isolation, automatic merging of any kind, and a model tool for labels, assignees, reopen or edit.
