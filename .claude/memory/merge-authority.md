---
name: merge-authority
description: "The user lets me merge PRs in always-on-agent myself (\"You may merge as you like\"); how to do it safely"
metadata:
  node_type: memory
  type: feedback
  originSessionId: c6eb61f7-e44f-4f43-b6d7-9ad8758f82a9
  modified: 2026-10-15T15:32:20.512Z
---

On 2026-10-15 the user said "You may merge as you like" for this repo. Before that, the rule was to merge only when explicitly told.

**Why:** a personal prototype where they want me to carry PRs through; I merged #50 and #51 under this.

**How to apply:** check the PR state first (open, not draft, checks and review findings dealt with, all fixes already pushed, see [[merge-click-races-fix-pushes]]), then `gh pr merge <N> --squash --delete-branch`. Never `--admin` or `--auto`. This covers merging PRs I develop here via gh. It does not change aoa's own rule that merging is never a model tool inside the agent: only a human Slack click on an approved card starts `MergePrWorkflow`. Related: [[pr-link-issues]].

**Standing instruction on reviews:** when aoa's own review of my PR arrives, take its recommendation: merge on APPROVE (squash, delete branch), fix on CHANGES REQUESTED. Low/medium findings on an APPROVE go in a follow-up (I folded #57's into the next branch). The review only starts ~5 min after the last push (settle window) and then waits for the 10-minute poll, so push everything before it.
