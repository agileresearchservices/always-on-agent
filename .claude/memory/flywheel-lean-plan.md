---
name: flywheel-lean-plan
description: Decisions for issue #1 (eval/improvement flywheel): lean, no Langfuse, three slices #54/#55/#56, evals stay manual
metadata:
  type: project
---

Issue #1 is now an umbrella. Plan (2026-10-15): SQLite is the trace store, Slack is the human gate, Langfuse/OTel stays a possible later sidecar. Slices, all merged 2026-10-15: #54 capture (a1fc11e), #55 propose eval cases (37643f2), #56 fix PR after an accepted case (006949b). Entirely human-triggered; verified live 2026-10-15 end to end, including a real Draft fix -> verify -> push -> PR (#60, closed unmerged; used AOA_FIX_USE_API_KEY=1 temporarily, since removed). The service has no Claude login of its own (stripped env = 'Not logged in', exit 0): a normal run needs AOA_CLAUDE_OAUTH_TOKEN (`claude setup-token`) in .env, which the owner has not set yet.

**Why:** a two-agent debate (lean vs Langfuse) converged on lean: Langfuse needs Postgres/ClickHouse/Redis/MinIO on a laptop and duplicates sensitive traces. The user chose: capture, propose, fix-PR in the first slice; **evals stay manual** (paid key), scheduling later.

**How to apply:** don't add Langfuse or schedule evals unless asked. Eval validation of a proposed case runs in a subprocess with only ANTHROPIC_API_KEY (the eval runner patches `gh_call` process-wide, so never run it inside the worker). Fix author must never get GitHub tokens; aoa never merges. See [[merge-authority]].

**Lesson from #59:** aoa's reviewer asked for changes five times, always on isolation of the fix author. What finally passed: no shell for the author, no fix code runs before the human reads the diff, push the exact verified commit. A macOS sandbox-exec profile was tried and dropped (reachable launchd, breaks git).

**Goal closed 2026-10-15:** the owner confirmed the flywheel plan is done. The OAuth-token / real-Slack-click fix run is an optional later check, not a requirement; don't treat it as open. Phase 6 (scheduled runs, baseline comparison) lives in issue #61.


**Unattended (#64, merged 2026-10-15, PR #65, follow-ups d7e8239):** two schedules run on the worker: `aoa-flag-scan` (24h, free, posts one digest only on new flags, never proposes) and `aoa-eval-run` (OFF unless `AOA_EVAL_SCHEDULE_HOURS`>0, costs ~$0.30/run). Eval errors alert once and back off a day on an unchanged setup; "no case passed" is an error only if a run raised; soft cases compare to the best of the last 5 green runs; a green run after an alert posts "green again". Proposals and fix PRs stay human-clicked. Known gaps (low, left on purpose): digest lost if the activity crashes between scan and push, flag_scan blocks the event loop, `_scrub` only strips Anthropic keys. Slack link unfurls are off in the bridge.
