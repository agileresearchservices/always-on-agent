---
name: dont-remind-app-and-key-rotation
description: User dropped two reminders - GitHub App setup for #25 and rotating pasted Anthropic keys; stop listing them as pending
metadata:
  type: feedback
---

On 2026-10-14 the user told me to remove two items from my "waiting on you" list: creating the GitHub App for the reviewer identity (#25) and rotating the two Anthropic API keys pasted into chat.

**Why:** They decided not to act on them now and dislike repeated nagging (see also [[merge-click-races-fix-pushes]]).

**How to apply:** Don't list these as pending or remind about them in summaries. Reviews keep posting as COMMENT; the code path stays dormant until `AOA_REVIEWER_*` is set. Mention again only if the user asks about reviewer identity or key hygiene.
