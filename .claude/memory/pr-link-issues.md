---
name: pr-link-issues
description: PRs should almost always link their issue with a closing keyword so merging auto-closes it
metadata:
  node_type: memory
  type: feedback
  originSessionId: c6eb61f7-e44f-4f43-b6d7-9ad8758f82a9
  modified: 2026-10-14T20:36:36.552Z
---

Every PR should reference the issue it addresses with `Closes #N` (or `Fixes #N`) in the body, so the issue auto-closes on merge into the default branch. If no issue exists, create one first. Use `Refs #N` only when the PR deliberately leaves the issue open.

**Why:** a test PR (#3) in always-on-agent had no link, so the issue it related to (#2) stayed open; the user pointed out PRs should almost always be linked. Closing an unmerged PR never closes an issue either way.

**How to apply:** when opening any PR (and when the agent's github tools gain PR creation), put the closing keyword in the body. Repo has `.github/pull_request_template.md`. Related: [[cowboy-mode-commit-to-main]] covers when PRs are used at all.
