# The core and the loop

Source: `aoa/core.py` (`AgentCore`), `aoa/loop.py` (`run_agent_loop`, `Ops`), `aoa/direct.py`, `aoa/workflows.py`
(`WorkflowOps`), `aoa/activities.py`, `aoa/store.py`. This is the heart of the project: the same agent logic runs durably
under Temporal and in-process in tests.

## The loop (`loop.py`)

```text
for step in range(max_steps):
    resp = ops.llm_step(conv, "<turn>.s<step>", role, depth)     # one model call
    if no tool_uses:           return (end_turn, or max_tokens if the final answer was cut off)
    if stop_reason == max_tokens:   every tool call gets an error and is NOT run (its input may be truncated)
    else:                      results = run the tools (spawn_helper calls start first and overlap)
    events = steering()        # new user events that arrived meanwhile
    ops.append_results(conv, step_key, results, events)           # tool results + steered events, one user message
return max_steps
```

- It does **no I/O and uses no clocks**, so it is safe inside a Temporal workflow. The only way out to the world is `Ops`.
- A truncated tool call is never executed: running half a command is worse than retrying it.
- `spawn_helper` is executed here, not by a handler: a helper is a child conversation with fresh context (`role="helper"`),
  allowed only from the main agent and only up to `AOA_MAX_HELPER_DEPTH`. A failed helper returns an error to the model.
- **Steering.** A user message that arrives mid-turn is not queued behind the turn: `steering()` returns it after the
  current tools finish and `append_results` adds it to the same user message as the tool results, so the model sees it at
  its next step.

## The `Ops` seam

```python
class Ops(Protocol):
    async def llm_step(self, conv, step_key, role, depth) -> dict
    async def run_tool(self, conv, role, depth, tool_use, trigger_user) -> dict
    async def append_results(self, conv, step_key, results, events) -> None
    async def spawn_helper(self, conv, tool_use, depth) -> str
```

| Implementation | Each method is | Used by |
|---|---|---|
| `workflows.WorkflowOps` | a Temporal activity (`spawn_helper`: a child workflow) | production |
| `direct.DirectOps` | a plain call to the matching `AgentCore` method | tests, no-server use |

Add a capability by adding the method to `AgentCore`, an activity wrapper in `activities.py`, and an `Ops` method on both
runners. Do not put it in the loop.

## `AgentCore` (`core.py`)

Each durable step is an async method that is **idempotent** through the store:

| Method | Does |
|---|---|
| `prepare_turn` | Adds the new user message with the volatile context block (time, scratchpad, recent log), records the turn trigger |
| `llm_step` | One model call under step key `llm:<turn>.s<N>`; stores the assistant message and result together; records usage and cache misses |
| `run_tool` | One tool call under step key `tool:<tool_use id>`; wraps, caps and stores the result |
| `append_results` | Adds one user message under `results:<step key>` holding the tool results and any steered events |
| `maybe_compact` | Summarises an old prefix when the context passes the limit |
| `warm_cache` | Sends a cheap request on the exact main prefix so the next real turn hits the cache |
| approval helpers | Nudge and expire pending approvals |

### Idempotency, precisely

`Store.commit_step(conv, step_key, result, role=..., content=...)` runs in one `BEGIN IMMEDIATE` transaction: if the step
exists it returns `(stored_result, fresh=False)` and writes nothing; otherwise it inserts the message **and** the
`steps` row together. So an activity that crashes after committing but before acknowledging is retried and gets the stored
answer: **no duplicate assistant message and no second billed model call.** `llm_step` and `run_tool` both check
`step_get` first.

### Retries

| What | Retried by Temporal? | Why |
|---|---|---|
| Model call (`llm_step`) | yes | No side effect; idempotent by step key |
| Tool (`run_tool`) | **no** (`NO_RETRY`) | It may have changed the world. A failure is reported to the model as "effects unknown" |
| State writes (`append_results`, ...) | yes | Idempotent by step key |

A tool handler therefore must be safe to be *not* retried, and the model must be able to cope with "it may or may not have
happened".

### Turn triggers

`kv turn_trigger` records whether a turn was started by the person or in the background. Tools read it as
`ToolContext.trigger` / `trigger_user`. Only a user-started turn may create a routine, and in such a turn `send_user`
turns an `update` into a `reply`, so quiet hours never hold an answer.

## Stop reasons

| `LoopResult.stop` | Meaning | What happens |
|---|---|---|
| `end_turn` | The model finished | Normal |
| `max_steps` | `AOA_MAX_STEPS` calls used, still working | `flag.scan` flags it as `max_steps:<turn>` |
| `max_tokens` | The final answer itself was cut off | Reported, and flagged by `flag.scan` |

## Testing the core

`direct.handle_events(core, session, events)` runs a full turn without a server. `tests/conftest.py::make_core(script)`
builds an `AgentCore` with a fixed clock (2026-10-14 14:00 New York) and a `FakeLLM` that replays `script`; each script
step is content blocks (`text(...)`, `tool(...)`), a callable, or an exception. Assert on the outbox, the stored messages
and the tool calls. Reserve `@pytest.mark.temporal` for behaviour only Temporal has.
