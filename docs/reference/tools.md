# Tools

Source: `aoa/tools.py` (`REGISTRY`, `ToolSpec`, `ToolContext`, `dispatch`). The model acts only through these. Reading
this page tells you what each tool does, who may call it, and what it is trusted with.

## How tools reach the model

- **Core tools** ship their full schema in every request. They are part of the cached prefix, so the set is fixed and
  ordered.
- **Deferred tools** appear only as a name and one line in the system prompt. The model calls `tool_search(query)` to read
  a schema (matching on name, description and `tags`), then `call_deferred(name, arguments)` to run it. This keeps the
  tool block small. Adding a deferred tool changes the prompt's name list, so expect one cache-miss warning after the
  worker restarts.
- `spawn_helper` is declared here for its schema but **executed by the loop**, not a handler: it starts a child
  `HelperWorkflow` with a fresh conversation, and only its short final result comes back.
- A helper (the role used for sub-agents and PR reviews) may call only tools with `helper_ok=True`. `dispatch` refuses the
  rest with an error the model can read.

## The tools

| Tool | Tier | Helper OK | Irreversible | What it does |
|---|---|---|---|---|
| `send_user` | core | no | no | The only way the person sees anything. Goes through `Notifier` (digest and quiet hours). Args: `text`, `kind`, `client_key` |
| `tool_search` | core | yes | no | Find deferred tools by keyword and read their schemas |
| `call_deferred` | core | yes | no | Invoke a deferred tool by name |
| `run_shell` | core | yes | no | Run a bash command in the sandbox directory. Every command is reviewed first (denylist, then a reviewer model that fails closed) |
| `read_file` | core | yes | no | Read a sandbox file, paged (`offset`, `limit`) |
| `write_file` | core | yes | no | Write a sandbox file |
| `memory_search` | core | yes | no | Search facts, dated logs and skills (regex or plain text) |
| `memory_read` | core | yes | no | Read a memory file by relative path |
| `memory_write` | core | yes | no | Write memory: a durable fact, replace all facts, append to the log, a scratchpad note with a TTL, a skill |
| `spawn_helper` | core | no | no | Delegate a self-contained task to a helper with fresh context |
| `web_fetch` | deferred | yes | no | HTTP GET of a **public** page (private addresses refused); text returned wrapped as untrusted |
| `report_issue` | deferred | no | no | Record that something went wrong in this conversation. Marked unverified, capped at 20 open per conversation, changes nothing else. Feeds [the flywheel](flywheel.md) |
| `schedule_routine` | deferred | no | no | Create a recurring prompt on a UTC cron. Needs the person's approval, and only in a turn the person started |
| `list_routines` | deferred | no | no | List routines |
| `delete_routine` | deferred | no | no | Delete a routine by name |
| `github_read` | deferred | yes | no | Read a PR (with diff), an issue, the comments on either, or a PR's CI checks (`what` = `pr` / `issue` / `comments` / `checks`). Output wrapped as untrusted |
| `github_list` | deferred | yes | no | List PRs and issues through the search API, newest activity first. Defaults: both kinds, open only. `limit` max 30 |
| `github_write` | deferred | no | **yes** | Create an issue, comment, close an issue or PR, or submit a PR review. **Needs an approval card** |
| `external_send` | deferred | no | **yes** | Send to an external channel (email / Slack). **Needs approval.** Prototype: the send is a stub that logs to the outbox |

All three GitHub tools refuse a repo outside `AOA_GITHUB_USER` **before making any request**. Not available to the
model: labels, assignees, reopen or edit, code or commit search, review-thread comments, workflow logs. There is no merge
tool, by design (see [trust-and-safety.md](trust-and-safety.md)).

## Runtime rules

1. **A tool runs at most once.** In the workflow, `run_tool` has no retry. If it fails, the model is told the effects are
   unknown and is not re-run. Model calls, by contrast, are retried. A new handler must be safe under that contract.
2. **Handlers raise `ToolError` with an actionable message.** The message is shown to the model, so say what to do next.
3. **Results over `AOA_TOOL_RESULT_MAX_CHARS` are spilled** to a sandbox file by `core._cap`, and the model gets the first half of the limit
   plus the sandbox path to page through with `read_file`.
4. **Anything from outside is wrapped** with `safety.wrap_untrusted` before it enters the context.
5. **Irreversible tools use the approval flow**: the handler writes an `approvals` row, sends an `approval` card, and
   only acts when called again with the resolved `approval_id`. An approval nudges once after `AOA_APPROVAL_NUDGE` and
   expires after `AOA_APPROVAL_EXPIRE`; an expired one cannot be approved late.
6. **Per-trigger limits** read `ToolContext.trigger` / `trigger_user` (for example only a user-started turn may create a
   routine).

## Adding or changing a tool: checklist

Learned shipping `github_list` (#66).

1. Register it in `REGISTRY`. Choose core or deferred (default to deferred), `helper_ok`, `irreversible`, and `tags`
   that match how the model will search for it.
2. Read-only handlers can be called with `ctx=None` for a live smoke test, with the token read from the environment:
   `set -a; . ./.env; set +a; .venv/bin/python -c ...`. Never print the token.
3. **Teach the eval fake GitHub** (`evals/runner.py::_fake_gh_call`) any new endpoint, or the case sees a generic reply.
4. Import any new grader in `evals/cases.py`, or collection of `test_evals.py` and `test_eval_proposal.py` breaks.
5. Write a test like `tests/test_edge.py::test_github_*`: monkeypatch `aoa.github_monitor.gh_call` (signature
   `method, path, json=None, accept=""`), call `REGISTRY[name].handler(ctx, args)`, and assert that a scope refusal makes
   no request.
6. Editing `tools.py` or `evals/` changes the scheduled-eval fingerprint, so a run happens next if the schedule is on.
7. Check one case on the real model: `python -m evals --runs 3 --case NAME` (about $0.05).
8. Add this page's table row. `tests/test_docs_in_sync.py` fails if a registered tool is missing from it.
