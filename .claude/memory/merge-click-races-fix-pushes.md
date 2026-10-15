---
name: merge-click-races-fix-pushes
description: The user clicks Slack Merge as soon as an approve card appears; fixes pushed after that miss the merge
metadata:
  node_type: memory
  type: feedback
  originSessionId: c6eb61f7-e44f-4f43-b6d7-9ad8758f82a9
  modified: 2026-10-14T23:08:23.187Z
---

The user clicks the aoa Slack [Merge] button as soon as an APPROVE card shows up (sometimes several at once), without waiting for me to address the bot's medium/low findings.

**Why:** On 2026-10-14, PR #22 was merged at its reviewed commit while my fix commit for the review's findings was still being pushed. The branch was deleted at merge, so the fix needed a follow-up issue/PR (#23/#24).

**How to apply:** Before pushing a PR whose review might approve it, finish every fix I intend to make, and push them together. If findings arrive after an APPROVE card is already up, expect the card to be clicked and plan a follow-up PR rather than assuming the branch will still be open. Related: [[pr-link-issues]].
