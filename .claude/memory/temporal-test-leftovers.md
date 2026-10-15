---
name: temporal-test-leftovers
description: Tests and capture scripts run against the live local Temporal; only session-main is a real long-running workflow
metadata:
  type: reference
---

The e2e tests (`tests/test_temporal_e2e.py`) and `tests/histories/capture.py` use the same Temporal dev server as the launchd services, on throwaway task queues (`aoa-test-*`, `aoa-capture-*`). Anything they leave running can never progress (no worker serves the queue) and shows up in the UI. Real running workflows are on task queue `aoa`: `session-main` (always), plus short PR/merge/poll runs.

**Why:** the user saw 16 stuck `session-t-*`, `session-cap-*` and `pr-review-t-*-demo-7` workflows (2026-10-15). Fixed the poll test to terminate its PrReviewWorkflow; the rest were old runs.

**How to apply:** after any test run that touches Temporal, `temporal workflow list --query 'ExecutionStatus="Running"'` should show only `session-main`. New e2e tests must end every workflow they start. Terminating leftovers needs the user's explicit OK (the auto-mode classifier blocked it once; the user then said "terminate them").
