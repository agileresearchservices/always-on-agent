---
name: new-tool-recipe
description: Checklist and gotchas for adding a model tool to aoa (registry, eval fake, graders import, live smoke test)
metadata:
  type: project
---

Adding a model tool (done for `github_list` / `github_read comments|checks`, #66, PR #67) needs: REGISTRY entry in `aoa/tools.py`; teach `evals/runner.py::_fake_gh_call` any new endpoint; import any new grader in `evals/cases.py`; a test patterned on `tests/test_edge.py::test_github_*` (patch `aoa.github_monitor.gh_call`); one real-model eval check (`python -m evals --runs 3 --case NAME`, ~$0.05); docs in CLAUDE.md and README.

**Why:** each step was a small trap (missing grader import broke test collection; the eval fake returned a generic reply for search).
**How to apply:** follow the list in CLAUDE.md ("Adding or changing a model tool"). Live smoke test of a read-only handler: `set -a; . ./.env; set +a`, call the handler with `ctx=None`; never print the token. Tool names the model can read GitHub with: scope is `AOA_GITHUB_USER` only; the `.env` token has full access to that account. See [[flywheel-lean-plan]] for the eval/flywheel context.
