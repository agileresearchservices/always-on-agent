# Trust and safety

Source: `aoa/safety.py`, `aoa/events.py`, `aoa/notify.py`, `aoa/tools.py`, `aoa/gh_reviewer.py`, `aoa/pr_merge.py`,
`aoa/pr_resolve.py`, `aoa/fix_pr.py`. The threat model: the agent reads text it does not control (email, webhooks, PR
descriptions, web pages) and can take actions. The design assumes that text will sometimes try to give it orders.

## Principles

1. **Only the authenticated person gives instructions.** An `Event` is `trusted` only for the user and approvals.
   Everything else is data, however it is worded.
2. **Dangerous capabilities need a human decision.** Approval cards for sends and GitHub writes; a human click for
   merges and fix PRs.
3. **The model never holds a credential** it could leak or misuse. The GitHub token lives in the worker.
4. **Fail closed.** If the command reviewer cannot decide, the command does not run.
5. **No security claim without a mechanism.** Where a control is missing, the docs say so (see "What this is not").

## Untrusted text

- `events.render_event_text` wraps any non-trusted event in a labelled block that tells the model it is data to reason
  about, never instructions or authority for irreversible actions.
- `safety.wrap_untrusted` does the same for tool output that carries outside text: `web_fetch`, `github_read`,
  `github_list`, PR bodies and diffs given to the reviewer.
- Text that goes *into Slack* from a model or a trace has Slack control characters escaped (`eval_proposal._esc`), and
  text in a fix-PR body is stripped of @-mentions, HTML, links and `#N`, so it cannot ping, link or auto-reference.

## Commands

`run_shell` commands pass two gates (`safety.py`):

1. A **denylist** of patterns refused outright.
2. A **reviewer model** (`AOA_REVIEW_MODEL`, Haiku) that returns a verdict under an API-enforced JSON schema
   (`REVIEW_SCHEMA`). It **fails closed**: an unclear answer or an API failure is a refusal, which the agent sees as
   "blocked by review: no clear approval".

`web_fetch` refuses non-public addresses (`url_is_public`). Network guardrails live in the same file.

## Actions that need a person

| Action | Gate |
|---|---|
| `external_send` | Approval card. Prototype: the send itself is a stub |
| `github_write` (issue, comment, close, PR review) | Approval card |
| `schedule_routine` | Approval, and only in a turn the person started (`trigger_user`) |
| **Merging a PR** | **Never a model tool.** Only a Slack [Merge] click on an approved review card starts `MergePrWorkflow`. The card carries an opaque offer id stored when the review was published; the workflow pins the reviewed commit |
| Conflict resolution push | Same click; the new commit needs its own review and its own click |
| Fix PR | [Draft fix] click, then a second click ([Run checks + open PR]) after the person has read `git diff main...fix/<slug>` |
| Accepting an eval case | [Accept] click; it commits to a local branch only, never main, never pushed |

An approval nudges once after `AOA_APPROVAL_NUDGE` (4 h) and expires after `AOA_APPROVAL_EXPIRE` (24 h), driven by
`SessionWorkflow` timers (guarded by `workflow.patched("approval-expiry")`). An expired approval cannot be approved late.
`approval_resolve` acts only on `pending`, so a double click decides once.

## Who is allowed in

- The HTTP API needs `AOA_API_TOKEN`; webhooks need `AOA_WEBHOOK_SECRET`. The server refuses to start without both
  unless `AOA_ALLOW_INSECURE=1` (development only).
- The Slack bridge accepts messages only from `SLACK_ALLOWED_USER_ID` in `SLACK_CHANNEL_ID`.
- `AOA_GITHUB_USER` scopes GitHub: the three tools refuse any other owner **before any request**; the poll queries only
  that user.

## GitHub identity and the merge path

- PR reviews post as a real APPROVE or REQUEST_CHANGES **only** through a separate reviewer identity
  (`gh_reviewer.py`: a GitHub App via `AOA_REVIEWER_APP_ID` + key file, or `AOA_REVIEWER_TOKEN`). It is off when unset and
  falls back to a comment review on any failure.
- `MergePrWorkflow` refuses while GitHub auto-merge is on (checked before cloning and again before the push), never
  pushes a clean merge (`no_conflict`: GitHub's status was stale), and requires every line outside the conflict hunks to
  survive verbatim (`finish_resolution`).
- The token reaches git only through `GIT_CONFIG_*` environment variables, never a command line or a file in the clone.
  Tests run with an allowlisted environment.
- Failure `action`s in `pr_merge.RETRYABLE` bring the Merge button back; `needs_human`, `stale` and `tests_failed` do not.

## Fix PRs: the order of trust

`fix_pr.py` runs a headless Claude Code session to edit files, then checks the result **statically**:

- the session has no shell: only Read/Edit/Write/Grep/Glob scoped to the worktree and denied under `/Users` and `~`;
- no GitHub token or API key in its environment; an explicit Claude credential only;
- `--max-budget-usd` caps its spend;
- the case text reaches its prompt only as delimited untrusted data;
- the result is refused if it touches `evals/`, `tests/histories/`, any `conftest.py` / `sitecustomize.py` / `*.pth`,
  `pyproject.toml`, `aoa/testing.py`, deletes or skips tests, or contains a secret;
- the commit is **local**; nothing runs until the person has read the diff and clicked. Only then are tests and evals run
  (no tokens or keys, throwaway HOME) and the branch pushed;
- a file in `GUARDRAIL_PATHS` (mirrored in `.github/CODEOWNERS`, tested) puts `[guardrail]` in the PR title.

## What this is not

- **Not a sandbox.** `sandbox.LocalSandbox` runs as your user in a directory. The test commands for conflict
  resolution and fix verification also run as your user. Container isolation (#21) was evaluated and skipped on purpose;
  a macOS `sandbox-exec` profile was tried and dropped because a hostile test can still reach launchd. The control is that
  **no code from a fix executes before a human has read it**, and the resolver is for your own PRs only.
- Flags see only mechanical failures, and proposals are validated with a small number of runs and are not held-out
  (see [flywheel.md](flywheel.md)).
- `_scrub` strips only Anthropic-shaped keys from scheduled-eval error text.
