"""PR review: verdict parsing, rendering, and which PRs get reviewed. No model, no network."""
from __future__ import annotations

import json

from aoa.github_monitor import reviews_to_start
from aoa.pr_review import (build_task, check_citations, diff_index, github_body, load_focus, no_mentions,
                           parse_verdict, slack_text)

PR = {"repo": "agileresearchservices/demo", "number": 7, "title": "Tidy", "author": "agileresearchservices",
      "url": "https://github.com/agileresearchservices/demo/pull/7", "sha": "abcdef1234567890", "why": "new"}


def test_parse_verdict_accepts_fenced_json_and_caps_fields():
    raw = "Here you go:\n```json\n" + json.dumps({
        "verdict": "request_changes", "summary": "x" * 5000,
        "findings": [{"severity": "low", "file": "a.py", "line": 3, "comment": "nit"},
                     {"severity": "high", "file": "b.py", "line": "7", "comment": "bug"},
                     {"severity": "weird", "comment": "odd"}, {"severity": "high"}],
    }) + "\n```"
    v = parse_verdict(raw)
    assert v["verdict"] == "request_changes" and len(v["summary"]) == 1200
    assert [f["severity"] for f in v["findings"]] == ["high", "low", "low"]  # sorted; missing comment dropped
    assert v["findings"][0]["line"] is None  # "7" is not an int


def test_parse_verdict_rejects_garbage_and_unknown_verdicts():
    assert parse_verdict("") is None
    assert parse_verdict("looks good to me!") is None
    assert parse_verdict('{"verdict": "merge_now", "summary": "s"}') is None
    assert parse_verdict('{"broken": ') is None
    assert parse_verdict('noise {"a": 1} then {"verdict": "approve", "summary": "ok"}')["verdict"] == "approve"


def test_mentions_are_neutralised_everywhere():
    s = no_mentions("ping @octocat and @org/team, mail a@b.com, <!channel> <@U123>")
    assert "@octocat" not in s and "@org" not in s and "a@b.com" in s
    assert "<!channel>" not in s and "<@U123>" not in s
    v = {"verdict": "approve", "summary": "cc @someone", "findings": [{"severity": "low", "file": "", "line": None, "comment": "hi @x"}]}
    assert "@someone" not in github_body(v) and "@x" not in github_body(v)
    assert "@someone" not in slack_text(v, PR, posted=True)


def test_rendering_states_recommendation_and_where_it_was_posted():
    v = {"verdict": "request_changes", "summary": "Adds X.", "findings": [
        {"severity": "high", "file": "a.py", "line": 4, "comment": "off by one"}]}
    body = github_body(v)
    assert "recommendation REQUEST CHANGES" in body and "`a.py:4`" in body and "comment review" in body
    assert "REQUEST CHANGES" in slack_text(v, PR, posted=True) and "Posted on GitHub" in slack_text(v, PR, True)
    assert "Not posted" in slack_text(v, PR, posted=False, note="GitHub post failed")
    assert "Review failed" in slack_text(None, PR, posted=False)


def test_task_names_the_pr_and_marks_content_untrusted():
    t = build_task(PR)
    assert '"repo": "agileresearchservices/demo"' in t and '"number": 7' in t and "abcdef123456" in t
    assert "UNTRUSTED" in t and '"verdict"' in t


def test_only_owner_authored_prs_are_reviewed_capped_and_the_rest_queued(tmp_path):
    from aoa.store import Store

    store = Store(tmp_path / "s.sqlite3")
    prs = [{**PR, "number": n, "author": "AgileResearchServices" if n != 2 else "stranger"} for n in range(1, 7)]
    assert [p["number"] for p in reviews_to_start(store, "main", prs, "agileresearchservices")] == [1, 3, 4]
    # the next poll announces nothing new, but the overflow still gets reviewed
    assert [p["number"] for p in reviews_to_start(store, "main", [], "agileresearchservices")] == [5, 6]
    assert reviews_to_start(store, "main", [], "agileresearchservices") == []
    # a newer commit on a queued PR replaces the queued one rather than reviewing both
    store.kv_set("main", "github_review_queue", [{**PR, "number": 9, "sha": "old"}])
    got = reviews_to_start(store, "main", [{**PR, "number": 9, "sha": "new"}], "agileresearchservices")
    assert [(p["number"], p["sha"]) for p in got] == [(9, "new")]


def test_high_severity_finding_forces_request_changes():
    v = parse_verdict('{"verdict": "approve", "summary": "fine", "findings": [{"severity": "high", "comment": "leaks key"}]}')
    assert v["verdict"] == "request_changes"


def test_links_and_slack_syntax_from_the_model_are_defanged():
    v = {"verdict": "approve", "summary": "see [docs](http://evil.example) and <http://x|label> ![img](http://t/p.png)",
         "findings": [{"severity": "low", "file": "`< !channel>`\nx.py", "line": 1, "comment": "ok"}]}
    v = parse_verdict(json.dumps(v))
    body, card = github_body(v), slack_text(v, PR, posted=True)
    assert "](http" not in body and "docs (http://evil.example)" in body
    assert "<http://x|label>" not in card and "&lt;http://x|label&gt;" in card and "](http://evil" not in card
    assert "channel" in card and "<!channel>" not in card and "`< !" not in card


DIFF = """diff --git a/app/auth.py b/app/auth.py
index 1..2 100644
--- a/app/auth.py
+++ b/app/auth.py
@@ -10,3 +10,4 @@ def f():
 a
-b
+c
+d
 e
@@ -40 +41 @@
-x
+y
diff --git a/old.py b/old.py
deleted file mode 100644
--- a/old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-gone
-too
diff --git a/dir/new file.txt b/dir/new file.txt
new file mode 100644
--- /dev/null
+++ b/dir/new file.txt
@@ -0,0 +1,2 @@
+one
++++ b/not-a-header
"""


def test_diff_index_lists_the_new_side_ranges():
    idx = diff_index(DIFF)
    assert idx["app/auth.py"] == [(10, 13), (41, 41)]
    assert idx["old.py"] == []  # deleted: nothing on the new side
    assert idx["dir/new file.txt"] == [(1, 2)] and "not-a-header" not in idx


def _v(*findings, verdict="request_changes"):
    return {"verdict": verdict, "summary": "s", "findings": [
        {"severity": sev, "file": f, "line": line, "comment": c} for sev, f, line, c in findings]}


def test_check_citations_drops_foreign_files_and_blanks_unseen_lines():
    v = check_citations(_v(("high", "app/auth.py", 12, "ok"), ("medium", "app/auth.py", 30, "unseen line"),
                           ("high", "elsewhere.py", 3, "foreign file"), ("low", "", None, "general"),
                           ("low", "./app/auth.py", 41, "dot-slash")), diff_index(DIFF))
    assert [f["comment"] for f in v["findings"]] == ["ok", "unseen line", "general", "dot-slash"]
    assert [f["line"] for f in v["findings"]] == [12, None, None, 41] and v["dropped"] == 1 and v["unplaced"] == 1
    assert v["findings"][3]["file"] == "app/auth.py"


def test_a_dropped_finding_never_turns_into_an_approval():
    only_foreign = check_citations(_v(("high", "elsewhere.py", 1, "x")), diff_index(DIFF))
    assert only_foreign["findings"] == [] and only_foreign["verdict"] == "request_changes"
    assert "dropped" in github_body(only_foreign) and "dropped" not in github_body(_v())


def test_focus_is_read_from_the_local_profile_only(tmp_path):
    d = tmp_path / "review-focus"
    d.mkdir()
    (d / "agileresearchservices__demo.md").write_text("Watch for cross-tenant leaks.\n" + "x" * 5000)
    got = load_focus(tmp_path, "agileresearchservices/demo")
    assert got.startswith("Watch for cross-tenant leaks.") and len(got) == 3000
    assert load_focus(tmp_path, "agileresearchservices/other") == ""
    assert load_focus(tmp_path, "../../etc/passwd") == ""
    task = build_task(PR | {"review_focus": got})
    assert "standing notes" in task and "cross-tenant leaks" in task
    assert "standing notes" not in build_task(PR)


def test_task_carries_the_checklist():
    t = build_task(PR)
    assert "skip or return early" in t and "tests only" in t and "quietly reverts" in t
