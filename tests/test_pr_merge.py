"""Merge flow (stage 2): the decision table, the report text, the activities against a fake GitHub, and the
server's merge endpoint. No model, no Temporal server (see test_temporal_e2e.py for the workflow)."""
from __future__ import annotations

import json

import httpx
import pytest

from aoa.pr_merge import check_summary, decide, report_text

SHA = "abc123def4567890"
OFFER = {"repo": "agileresearchservices/demo", "number": 7, "sha": SHA, "title": "Tidy up",
         "url": "https://github.com/agileresearchservices/demo/pull/7", "verdict": "approve", "card_text": "card"}


def ghpr(**kw):
    pr = {"state": "open", "merged": False, "draft": False, "mergeable": True, "mergeable_state": "clean",
          "title": "Tidy up", "merge_commit_sha": None,
          "head": {"sha": SHA, "ref": "feat/x", "repo": {"full_name": "agileresearchservices/demo"}},
          "base": {"ref": "main", "repo": {"full_name": "agileresearchservices/demo", "default_branch": "main"}}}
    return pr | kw


# ---- decision table ----------------------------------------------------------------------------
def test_no_ci_is_neither_failing_nor_pending():
    # GitHub's combined status says "pending" when a commit has no statuses at all
    assert check_summary({"state": "pending", "total_count": 0, "statuses": []}, {"total_count": 0, "check_runs": []}) == ([], [])


def test_check_summary_reads_statuses_and_check_runs():
    status = {"state": "failure", "total_count": 2,
              "statuses": [{"context": "ci/lint", "state": "failure"}, {"context": "ci/e2e", "state": "pending"}]}
    runs = {"check_runs": [{"name": "test", "status": "completed", "conclusion": "success"},
                           {"name": "build", "status": "completed", "conclusion": "timed_out"},
                           {"name": "deploy", "status": "in_progress", "conclusion": None},
                           {"name": "docs", "status": "completed", "conclusion": "skipped"}]}
    assert check_summary(status, runs) == (["ci/lint", "build"], ["ci/e2e", "deploy"])


@pytest.mark.parametrize("pr,failing,pending,action", [
    (ghpr(), [], [], "merge"),
    (ghpr(mergeable_state="has_hooks"), [], [], "merge"),
    (ghpr(merged=True, state="closed"), [], [], "already_merged"),
    (ghpr(state="closed"), [], [], "closed"),
    (ghpr(head={"sha": "fff", "ref": "x", "repo": None}), [], [], "stale"),
    (ghpr(draft=True), [], [], "draft"),
    (ghpr(), ["build"], ["e2e"], "checks_failed"),
    (ghpr(), [], ["e2e"], "wait"),
    (ghpr(mergeable=None, mergeable_state="unknown"), [], [], "wait"),
    (ghpr(mergeable=False, mergeable_state="dirty"), [], [], "conflict"),
    (ghpr(mergeable_state="behind"), [], [], "update_branch"),
    (ghpr(mergeable_state="blocked"), [], [], "blocked"),
    (ghpr(mergeable_state="unstable"), [], [], "checks_failed"),
])
def test_decide(pr, failing, pending, action):
    assert decide(pr, SHA, failing, pending)[0] == action


# ---- report text ----------------------------------------------------------------------------
def test_report_merged_lists_branch_and_issues():
    out = {"status": "merged", "merge_sha": "9f8e7d6c5b", "branch": {"ref": "feat/x", "result": "deleted"},
           "issues": [{"repo": "agileresearchservices/demo", "number": 3, "state": "CLOSED"},
                      {"repo": "agileresearchservices/demo", "number": 4, "state": "OPEN"}], "note": ""}
    t = report_text(OFFER, out)
    assert t.startswith(":tada: *Merged*") and "`9f8e7d6`" in t and "deleted" in t
    assert "demo#3 closed" in t and "demo#4 is still open" in t


def test_report_merged_without_linked_issue_warns():
    t = report_text(OFFER, {"status": "merged", "merge_sha": "", "issues": [], "branch": None})
    assert "No linked issue" in t


def test_report_failure_offers_retry_only_when_it_can_help():
    assert "try again" in report_text(OFFER, {"status": "failed", "action": "checks_failed", "reason": "failing checks: x"})
    assert "try again" not in report_text(OFFER, {"status": "failed", "action": "stale", "reason": "new commits"})
    for action in ("no_conflict", "auto_merge", "resolve_failed"):
        assert "try again" in report_text(OFFER, {"status": "failed", "action": action, "reason": "r"})
    t = report_text(OFFER, {"status": "failed", "action": "tests_failed", "reason": "tests failed"})
    assert "try again" not in t and "Fix the tests" in t


def test_report_resolved_lists_files_tests_and_next_step():
    out = {"status": "resolved", "new_sha": "1234567890", "files": ["app.py"], "summaries": {"app.py": "kept both"},
           "tests": "passed", "test_cmd": 'pip install -e ".[dev]" && pytest -k `x`'}
    t = report_text(OFFER, out)
    assert "Resolved merge conflicts" in t and "`1234567`" in t and "not merged" in t
    assert "app.py" in t and "kept both" in t and "new card" in t
    assert """Tests: passed (`pip install -e ".[dev]" &amp;&amp; pytest -k 'x'`)""" in t  # escaped, not mangled


def test_reviewer_is_told_about_an_aoa_resolution():
    from aoa.pr_review import build_task

    pr = {"repo": OFFER["repo"], "number": 7, "sha": SHA}
    assert "resolved by a model" not in build_task(pr)
    assert "resolved by a model (files: app.py)" in build_task(pr | {"resolved_files": ["app.py"]})


def test_report_escapes_titles():
    t = report_text(OFFER | {"title": "<!channel> [WIP] fix tool_choice"}, {"status": "updated"})
    assert "<!channel>" not in t and "&lt;!channel&gt; (WIP) fix tool_choice](" in t


# ---- activities against a fake GitHub ---------------------------------------------------------
class FakeGitHub:
    """Routes gh_call by (method, path prefix); records every call."""

    def __init__(self, routes):
        self.routes, self.calls, self.tokens = routes, [], []

    async def __call__(self, method, path, json=None, accept="", token=None):
        self.calls.append((method, path, json))
        self.tokens.append(token)
        kind = "DIFF" if method == "GET" and "diff" in accept else method  # a diff is routed as ("DIFF", path)
        for (m, prefix), resp in self.routes.items():
            if m == kind and path.startswith(prefix):
                resp = resp(json) if callable(resp) else resp
                if isinstance(resp, Exception):
                    raise resp
                code, body = resp
                req = httpx.Request(method, "https://api.github.com" + path)
                r = httpx.Response(code, text=body, request=req) if isinstance(body, str) else httpx.Response(code, json=body, request=req)
                if code >= 400:
                    raise httpx.HTTPStatusError("err", request=r.request, response=r)
                return r
        raise AssertionError(f"unexpected {method} {path}")


@pytest.fixture
def acts(make_core, monkeypatch):
    from aoa.activities import AgentActivities

    monkeypatch.setenv("AOA_GITHUB_USER", "agileresearchservices")
    core, _ = make_core([])
    return AgentActivities(core, None, core.s)


PR7 = "/repos/agileresearchservices/demo/pulls/7"
REVIEW_READS = {("GET", PR7): (200, ghpr()), ("DIFF", PR7): (200, "")}  # what publish_pr_review reads before posting


def posts(fake):
    return [c for c in fake.calls if c[0] == "POST"]


def use(monkeypatch, fake):
    from aoa import github_monitor
    monkeypatch.setattr(github_monitor, "gh_call", fake)
    return fake


async def test_inspect_collects_checks_and_linked_issues(acts, monkeypatch):
    gql = {"data": {"repository": {"pullRequest": {"closingIssuesReferences": {"nodes": [
        {"number": 3, "state": "OPEN", "repository": {"nameWithOwner": "agileresearchservices/demo"}}]}}}}}
    use(monkeypatch, FakeGitHub({
        ("GET", "/repos/agileresearchservices/demo/pulls/7"): (200, ghpr()),
        ("GET", f"/repos/agileresearchservices/demo/commits/{SHA}/status"): (200, {"state": "pending", "total_count": 0}),
        ("GET", f"/repos/agileresearchservices/demo/commits/{SHA}/check-runs"): (200, {"check_runs": []}),
        ("POST", "/graphql"): (200, gql),
    }))
    st = await acts.merge_inspect({"repo": OFFER["repo"], "number": 7, "sha": SHA})
    assert st["action"] == "merge" and st["head_ref"] == "feat/x" and st["same_repo"] and st["default_base"]
    assert st["protected_refs"] == ["main", "main"]
    assert st["issues"] == [{"repo": "agileresearchservices/demo", "number": 3, "state": "OPEN"}]


async def test_inspect_refuses_out_of_scope_repo(acts, monkeypatch):
    from aoa.github_monitor import GithubError

    fake = use(monkeypatch, FakeGitHub({}))
    with pytest.raises(GithubError):
        await acts.merge_inspect({"repo": "nasuni/hyrule", "number": 1, "sha": SHA})
    assert fake.calls == []


async def test_merge_is_squash_pinned_to_the_reviewed_commit(acts, monkeypatch):
    fake = use(monkeypatch, FakeGitHub({
        ("GET", "/repos/agileresearchservices/demo/pulls/7"): (200, ghpr()),
        ("PUT", "/repos/agileresearchservices/demo/pulls/7/merge"): (200, {"merged": True, "sha": "m3rg3"}),
    }))
    res = await acts.merge_pr({"repo": OFFER["repo"], "number": 7, "sha": SHA, "title": "Tidy up"})
    assert res == {"merged": True, "merge_sha": "m3rg3"}
    put = [c for c in fake.calls if c[0] == "PUT"][0]
    assert put[2] == {"merge_method": "squash", "sha": SHA, "commit_title": "Tidy up (#7)"}


async def test_merge_retry_after_success_does_not_merge_twice(acts, monkeypatch):
    fake = use(monkeypatch, FakeGitHub({
        ("GET", "/repos/agileresearchservices/demo/pulls/7"): (200, ghpr(merged=True, state="closed", merge_commit_sha="m3rg3")),
    }))
    res = await acts.merge_pr({"repo": OFFER["repo"], "number": 7, "sha": SHA, "title": "Tidy up"})
    assert res == {"merged": True, "merge_sha": "m3rg3"} and [c[0] for c in fake.calls] == ["GET"]


async def test_merge_error_that_actually_merged_counts_as_merged(acts, monkeypatch):
    state = {"n": 0}

    def pr_view(_):
        state["n"] += 1
        return (200, ghpr() if state["n"] == 1 else ghpr(merged=True, state="closed", merge_commit_sha="m3rg3"))

    use(monkeypatch, FakeGitHub({
        ("GET", "/repos/agileresearchservices/demo/pulls/7"): pr_view,
        ("PUT", "/repos/agileresearchservices/demo/pulls/7/merge"): (502, {"message": "bad gateway"}),
    }))
    res = await acts.merge_pr({"repo": OFFER["repo"], "number": 7, "sha": SHA, "title": "Tidy up"})
    assert res["merged"] is True


async def test_merge_refused_for_changed_head(acts, monkeypatch):
    use(monkeypatch, FakeGitHub({
        ("GET", "/repos/agileresearchservices/demo/pulls/7"): (200, ghpr()),
        ("PUT", "/repos/agileresearchservices/demo/pulls/7/merge"): (409, {"message": "Head branch was modified."}),
    }))
    res = await acts.merge_pr({"repo": OFFER["repo"], "number": 7, "sha": SHA, "title": "Tidy up"})
    assert res["merged"] is False and "409" in res["reason"] and "Head branch was modified" in res["reason"]


async def test_merge_5xx_is_retried_when_not_merged(acts, monkeypatch):
    use(monkeypatch, FakeGitHub({
        ("GET", "/repos/agileresearchservices/demo/pulls/7"): (200, ghpr()),
        ("PUT", "/repos/agileresearchservices/demo/pulls/7/merge"): (503, {"message": "busy"}),
    }))
    with pytest.raises(httpx.HTTPStatusError):
        await acts.merge_pr({"repo": OFFER["repo"], "number": 7, "sha": SHA, "title": "Tidy up"})


async def test_delete_branch(acts, monkeypatch):
    fake = use(monkeypatch, FakeGitHub({
        ("DELETE", "/repos/agileresearchservices/demo/git/refs/heads/feat/x%23y"): (204, None),
        ("DELETE", "/repos/agileresearchservices/demo/git/refs/heads/gone"): (422, {"message": "Reference does not exist"}),
    }))
    a = {"repo": OFFER["repo"], "same_repo": True}
    assert (await acts.merge_delete_branch(a | {"ref": "feat/x#y"}))["result"] == "deleted"
    assert (await acts.merge_delete_branch(a | {"ref": "gone"}))["result"] == "deleted"
    assert "fork" in (await acts.merge_delete_branch(a | {"ref": "x", "same_repo": False}))["result"]
    assert "base" in (await acts.merge_delete_branch(a | {"ref": "main", "protected_refs": ["main"]}))["result"]
    assert len(fake.calls) == 2


async def test_issue_states_stay_in_scope(acts, monkeypatch):
    fake = use(monkeypatch, FakeGitHub({("GET", "/repos/agileresearchservices/demo/issues/3"): (200, {"state": "closed"})}))
    got = await acts.merge_issue_states([{"repo": "agileresearchservices/demo", "number": 3, "state": "OPEN"},
                                         {"repo": "nasuni/hyrule", "number": 9, "state": "OPEN"}])
    assert [i["state"] for i in got] == ["CLOSED", "UNKNOWN"] and len(fake.calls) == 1


async def test_publish_approve_creates_one_stable_merge_offer(acts, monkeypatch):
    use(monkeypatch, FakeGitHub(REVIEW_READS | {("POST", "/repos/agileresearchservices/demo/pulls/7/reviews"): (200, {})}))
    pr = {k: OFFER[k] for k in ("repo", "number", "sha", "title", "url")} | {"author": "agileresearchservices"}
    ok = '{"verdict": "approve", "summary": "Fine.", "findings": []}'
    await acts.publish_pr_review({"session": "main", "pr": pr, "review_text": ok})
    await acts.publish_pr_review({"session": "main", "pr": pr, "review_text": ok})  # retried activity
    card = [o for o in acts.core.store.outbox_since("main") if o["kind"] == "pr_review"]
    assert len(card) == 1
    oid = card[0]["payload"]["merge_offer"]
    offer = acts.core.store.kv_get("main", "merge-offer:" + oid)
    assert offer["sha"] == SHA and offer["verdict"] == "approve" and offer["card_text"] == card[0]["payload"]["text"]

    bad = '{"verdict": "request_changes", "summary": "Bug.", "findings": []}'
    await acts.publish_pr_review({"session": "main", "pr": pr | {"sha": "f" * 16}, "review_text": bad})
    last = acts.core.store.outbox_since("main")[-1]
    assert "merge_offer" not in last["payload"]


# ---- server endpoint ------------------------------------------------------------------------
@pytest.fixture
def api(monkeypatch):
    import os
    import tempfile
    os.environ.setdefault("AOA_DATA_DIR", tempfile.mkdtemp())
    from fastapi.testclient import TestClient

    import aoa.server as server

    class FakeTemporal:
        def __init__(self):
            self.started, self.running = [], set()

        async def start_workflow(self, workflow, arg, **kw):
            from temporalio.exceptions import WorkflowAlreadyStartedError
            if kw["id"] in self.running:
                raise WorkflowAlreadyStartedError(kw["id"], workflow)
            self.running.add(kw["id"])
            self.started.append({"workflow": workflow, "arg": arg, **kw})

    monkeypatch.delenv("AOA_API_TOKEN", raising=False)
    monkeypatch.setenv("AOA_GITHUB_USER", "agileresearchservices")
    fake = FakeTemporal()
    server.app.state.client = fake
    return TestClient(server.app), fake, server.store


def test_merge_endpoint_starts_one_workflow_for_a_stored_offer(api):
    from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

    client, fake, store = api
    store.kv_set("main", "merge-offer:mrg-0123456789ab", OFFER)
    r = client.post("/sessions/main/merges/mrg-0123456789ab", json={"card_ts": "17.5"})
    assert r.status_code == 202
    call = fake.started[0]
    assert call["workflow"] == "MergePrWorkflow" and call["id"] == "merge-main-mrg-0123456789ab"
    assert call["arg"]["offer"]["sha"] == SHA and call["arg"]["card_ts"] == "17.5"
    assert call["id_conflict_policy"] == WorkflowIDConflictPolicy.FAIL
    assert call["id_reuse_policy"] == WorkflowIDReusePolicy.ALLOW_DUPLICATE
    sa = {p.key.name: p.value for p in call["search_attributes"]}
    assert sa == {"Repo": OFFER["repo"], "PrNumber": OFFER["number"], "Sha": SHA, "Session": "main"}
    assert client.post("/sessions/main/merges/mrg-0123456789ab", json={}).status_code == 409  # double click


def test_merge_endpoint_rejects_unknown_wrong_session_and_out_of_scope(api):
    client, fake, store = api
    store.kv_set("main", "merge-offer:mrg-aaaaaaaaaaaa", OFFER)
    store.kv_set("main", "merge-offer:mrg-bbbbbbbbbbbb", OFFER | {"repo": "nasuni/hyrule"})
    store.kv_set("main", "merge-offer:mrg-cccccccccccc", OFFER | {"verdict": "request_changes"})
    assert client.post("/sessions/main/merges/mrg-ffffffffffff", json={}).status_code == 404
    assert client.post("/sessions/other/merges/mrg-aaaaaaaaaaaa", json={}).status_code == 404
    assert client.post("/sessions/main/merges/merge-offer:x", json={}).status_code == 404
    assert client.post("/sessions/main/merges/mrg-bbbbbbbbbbbb", json={}).status_code == 403
    assert client.post("/sessions/main/merges/mrg-cccccccccccc", json={}).status_code == 404
    assert fake.started == []


async def test_resolve_refuses_while_auto_merge_is_on(acts, monkeypatch):
    fake = use(monkeypatch, FakeGitHub({
        ("GET", "/repos/agileresearchservices/demo/pulls/7"): (200, ghpr() | {"auto_merge": {"merge_method": "squash"}}),
    }))
    out = await acts.resolve_conflicts({"session": "main", "repo": OFFER["repo"], "number": 7, "sha": SHA,
                                        "head_ref": "feat/x", "base_ref": "main", "same_repo": True,
                                        "protected_refs": ["main"]})
    assert out["action"] == "auto_merge" and "auto-merge is on" in out["reason"]
    assert [c[0] for c in fake.calls] == ["GET"]  # stopped before cloning anything


def test_check_names_are_escaped_exactly_once():
    failing, _ = check_summary({}, {"check_runs": [{"name": "Lint & Test", "status": "completed", "conclusion": "failure"}]})
    action, reason = decide(ghpr(), SHA, failing, [])
    t = report_text(OFFER, {"status": "failed", "action": action, "reason": reason})
    assert "Lint &amp; Test" in t and "&amp;amp;" not in t


async def test_reviewer_identity_posts_a_real_approval(acts, monkeypatch):
    monkeypatch.setenv("AOA_REVIEWER_TOKEN", "ghs_reviewer")
    fake = use(monkeypatch, FakeGitHub(REVIEW_READS | {("POST", "/repos/agileresearchservices/demo/pulls/7/reviews"): (200, {})}))
    pr = {k: OFFER[k] for k in ("repo", "number", "sha", "title", "url")} | {"author": "agileresearchservices"}
    await acts.publish_pr_review({"session": "main", "pr": pr, "review_text": '{"verdict": "approve", "summary": "Fine.", "findings": []}'})
    sent = posts(fake)
    assert sent[0][2]["event"] == "APPROVE" and fake.tokens[-1] == "ghs_reviewer"
    assert "recommendation" not in sent[0][2]["body"] and sent[0][2]["commit_id"] == SHA
    card = acts.core.store.outbox_since("main")[-1]["payload"]
    assert "Posted on GitHub as an approval" in card["text"] and card["merge_offer"]


async def test_reviewer_identity_failure_falls_back_to_a_comment_review(acts, monkeypatch):
    monkeypatch.setenv("AOA_REVIEWER_TOKEN", "ghs_reviewer")

    def post(body):
        return (422, {"message": "nope"}) if body["event"] != "COMMENT" else (200, {})

    fake = use(monkeypatch, FakeGitHub(REVIEW_READS | {("POST", "/repos/agileresearchservices/demo/pulls/7/reviews"): post}))
    pr = {k: OFFER[k] for k in ("repo", "number", "sha", "title", "url")} | {"author": "agileresearchservices"}
    bad = '{"verdict": "request_changes", "summary": "Bug.", "findings": []}'
    await acts.publish_pr_review({"session": "main", "pr": pr, "review_text": bad})
    assert [c[2]["event"] for c in posts(fake)] == ["REQUEST_CHANGES", "COMMENT"]
    assert [t for c, t in zip(fake.calls, fake.tokens) if c[0] == "POST"] == ["ghs_reviewer", None]  # the fallback uses GITHUB_TOKEN
    text = acts.core.store.outbox_since("main")[-1]["payload"]["text"]
    assert "as a comment review" in text and "could not post (422)" in text


def test_merge_still_starts_when_the_server_does_not_know_the_search_attributes(api):
    from temporalio.service import RPCError, RPCStatusCode

    client, fake, store = api
    real = fake.start_workflow

    async def picky(workflow, arg, **kw):
        if "search_attributes" in kw:
            raise RPCError("rejected", RPCStatusCode.INVALID_ARGUMENT, b"")
        return await real(workflow, arg, **kw)

    fake.start_workflow = picky
    store.kv_set("main", "merge-offer:mrg-0123456789ab", OFFER)
    assert client.post("/sessions/main/merges/mrg-0123456789ab", json={}).status_code == 202
    assert fake.started[0]["workflow"] == "MergePrWorkflow" and "search_attributes" not in fake.started[0]


def test_merge_starts_even_if_the_stored_offer_lacks_a_field(api):
    client, fake, store = api
    store.kv_set("main", "merge-offer:mrg-0123456789ab", {k: v for k, v in OFFER.items() if k != "sha"})
    assert client.post("/sessions/main/merges/mrg-0123456789ab", json={}).status_code == 202
    assert "search_attributes" not in fake.started[0]


# ---- citation check in publish_pr_review ----------------------------------------------------
DIFF = """diff --git a/app/auth.py b/app/auth.py
--- a/app/auth.py
+++ b/app/auth.py
@@ -10,3 +10,3 @@
 a
-b
+c
 d
"""


async def test_publish_drops_findings_the_diff_cannot_support(acts, monkeypatch):
    fake = use(monkeypatch, FakeGitHub(REVIEW_READS | {("DIFF", PR7): (200, DIFF),
                                                       ("POST", PR7 + "/reviews"): (200, {})}))
    pr = {k: OFFER[k] for k in ("repo", "number", "sha", "title", "url")} | {"author": "agileresearchservices"}
    text = json.dumps({"verdict": "approve", "summary": "Fine.", "findings": [
        {"severity": "low", "file": "app/auth.py", "line": 11, "comment": "kept"},
        {"severity": "low", "file": "app/auth.py", "line": 99, "comment": "line never shown"},
        {"severity": "medium", "file": "other.py", "line": 1, "comment": "file not in the PR"}]})
    await acts.publish_pr_review({"session": "main", "pr": pr, "review_text": text})
    body = posts(fake)[0][2]["body"]
    assert "kept" in body and "line never shown" in body and "file not in the PR" not in body
    assert "`app/auth.py:99`" not in body and "1 finding(s) cited files outside" in body
    assert posts(fake)[0][2]["event"] == "COMMENT"
    card = acts.core.store.outbox_since("main")[-1]["payload"]
    assert card["verdict"] == "approve" and "were dropped" in card["text"]  # a drop never changes the verdict


async def test_publish_skips_the_check_when_the_head_moved_or_the_diff_is_unavailable(acts, monkeypatch):
    pr = {k: OFFER[k] for k in ("repo", "number", "sha", "title", "url")} | {"author": "agileresearchservices"}
    text = json.dumps({"verdict": "request_changes", "summary": "Bug.", "findings": [
        {"severity": "medium", "file": "other.py", "line": 1, "comment": "kept anyway"}]})
    moved = {"head": {"sha": "f" * 16}}
    for reads in ({("GET", PR7): (200, ghpr() | moved), ("DIFF", PR7): (200, DIFF)},
                  {("GET", PR7): (200, ghpr()), ("DIFF", PR7): (406, {"message": "diff too large"})},
                  {("GET", PR7): (404, {"message": "gone"})}):
        fake = use(monkeypatch, FakeGitHub(reads | {("POST", PR7 + "/reviews"): (200, {})}))
        acts.core.store.kv_set("main", f"prreview-posted:{pr['repo']}#7@{SHA}", None)
        await acts.publish_pr_review({"session": "main", "pr": pr, "review_text": text})
        assert "kept anyway" in posts(fake)[0][2]["body"]
