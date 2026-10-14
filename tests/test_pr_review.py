"""PR review: verdict parsing, rendering, and which PRs get reviewed. No model, no network."""
from __future__ import annotations

import json

from aoa.github_monitor import reviews_to_start
from aoa.pr_review import build_task, github_body, no_mentions, parse_verdict, slack_text

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
