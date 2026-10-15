# Prompt and prompt cache

Source: `aoa/prompt.py`, `aoa/llm.py` (`place_cache_breakpoints`), `aoa/core.py` (`llm_step`, `_setup`, `warm_cache`),
`aoa/compaction.py`. One rule governs it: **never break the cached prefix.**

## Why it matters

The model reads the system prompt, the tool block and the whole conversation on every call. Anthropic's prompt cache lets
a byte-identical prefix be read at a fraction of the price, but only while it is unchanged and within its lifetime. This
agent makes many calls per turn, so a stable prefix is most of the cost control. A cache miss within the lifetime is
treated as a **bug**: `core.llm_step` logs a warning for it, and `flag.scan` records it.

## The prefix is fixed and ordered

- The system prompt is a list of sections in a fixed order (`prompt.SECTION_ORDER`): `identity`, `operating_rules`,
  `safety`, `tool_index`, `core_facts`. Each section is hashed; the hash of the whole prefix is what tests and the
  cache-miss detector compare. **Add new stable sections at the end of the list, never in the middle.**
- The tool block is fixed and ordered too: core tools with full schemas, deferred tools by name only in `tool_index`.
  Adding a deferred tool changes that list, so expect exactly one cache miss after the next worker restart.
- **Volatile context never goes in the system prompt.** Time, scratchpad and recent log go into the *new user message*
  through `volatile_context_block`, when that message is first appended. After that the message is byte-stable history.

## Breakpoints

`place_cache_breakpoints` returns copies with at most four `cache_control` markers: the last tool, the last system
block, and the last content block of the last message (so each new turn pays only for the new part). Stale markers inside
history are stripped first. Two details that matter:

- A `thinking` or `redacted_thinking` block can never carry a breakpoint, so the marker goes on the last block that can.
- **Thinking blocks are kept verbatim** in the stored history. The API rejects a changed or reordered one.
- Every breakpoint gets the same `ttl` (`AOA_CACHE_TTL`: 300 or 3600). A longer lifetime must not follow a shorter one.

## Lifetime and cost

`AOA_CACHE_TTL` defaults to 300 seconds (5 minutes). One hour costs more to write (2x base input instead of 1.25x), so it
only pays when calls are mostly 5 to 60 minutes apart. Measured on this agent's own usage, 97.6% of calls follow another
within 5 minutes, and the hour would have cost about 16% more. The warm-up and compaction windows follow the TTL.

`python -m aoa usage` shows spend per day and per source, with cache read and write counts from the `usage` table.

## Warm-up and compaction reuse the exact prefix

`warm_cache` (fired when the person starts typing, signalled as `typing`) and compaction both build their request through
`_setup("main", 0)`, the same path as a real turn, so they hit the same cached prefix instead of creating a second one.

- **Compaction** triggers at `AOA_CONTEXT_HARD_LIMIT` tokens, or at `AOA_CONTEXT_WARM_COMPACT` while the cache is warm.
  It summarises an old prefix and replaces it (`Store.replace_prefix`).
- **Tool results** over `AOA_TOOL_RESULT_MAX_CHARS` are spilled to a sandbox file so they do not bloat every later call.

## Structured output

Where the code parses model JSON it asks the API to enforce a schema (`output_config.format`): the command reviewer
(`safety.REVIEW_SCHEMA`) and the PR-review conversations (`pr_review.VERDICT_SCHEMA`, applied by `core._output_for`). A
schema the API rejects is retried once without it, and the tolerant parsers still validate.

## Thinking

`AOA_REVIEW_THINKING` (an effort level; off by default) turns on adaptive thinking for the PR-review helper conversations
only. The current models reject `thinking.type=enabled` with a token budget, so effort is the only knob.

## Changing the prompt: what to expect

1. Edit `prompt.py`. Tests that compare fingerprints will change; that is expected.
2. After deploy, one cache miss warning per conversation is normal. More is a bug.
3. A long-running Slack conversation keeps habits from its earlier history even after a restart: when you change the
   prompt to fix a behaviour, **re-ask the question in a fresh state** to see the fix, and check it with an eval
   (`python -m evals --case NAME --runs 3`).
4. `aoa/prompt.py` is in the scheduled-eval fingerprint, so a change triggers a run if the schedule is on.
