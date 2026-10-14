"""GitHub monitor: announce every open non-draft PR once per head commit, with mocked GitHub."""
from __future__ import annotations

import httpx
import pytest

from aoa.github_monitor import GithubError, MAX_LISTED, notice_key, notice_text, poll_once, slack_safe
from aoa.store import Store

USER = "agileresearchservices"


class FakeGitHub:
    """Serves the search and PR endpoints from mutable state; records every request."""

    def __init__(self):
        self.prs: dict[int, dict] = {}  # number -> {"title","sha","updated","repo","author"}
        self.requests: list[str] = []
        self.status = 200

    def add(self, n, title="Fix thing", sha="aaa", updated="t1", repo=f"{USER}/demo", author="octo"):
        self.prs[n] = dict(title=title, sha=sha, updated=updated, repo=repo, author=author)

    def transport(self):
        def handler(req: httpx.Request) -> httpx.Response:
            self.requests.append(f"{req.method} {req.url.path}")
            if self.status != 200:
                return httpx.Response(self.status, json={})
            if req.url.path == "/search/issues":
                assert "draft:false" in req.url.params["q"] and f"user:{USER}" in req.url.params["q"]
                items = [{"repository_url": f"https://api.github.com/repos/{p['repo']}", "number": n,
                          "title": p["title"], "user": {"login": p["author"]}, "updated_at": p["updated"],
                          "html_url": f"https://github.com/{p['repo']}/pull/{n}"} for n, p in self.prs.items()]
                return httpx.Response(200, json={"items": items})
            n = int(req.url.path.rsplit("/", 1)[1])
            return httpx.Response(200, json={"head": {"sha": self.prs[n]["sha"]}})
        return httpx.MockTransport(handler)


@pytest.fixture
def gh():
    return FakeGitHub()


async def poll(gh, store):
    async with httpx.AsyncClient(transport=gh.transport()) as http:
        return await poll_once(http, store, "main", USER, "tok")


async def test_new_pr_announced_once_and_quiet_when_unchanged(gh, tmp_path):
    store = Store(tmp_path / "s.sqlite3")
    gh.add(1)
    first = await poll(gh, store)
    assert [(p["repo"], p["number"], p["why"]) for p in first] == [(f"{USER}/demo", 1, "new")]
    gh.requests.clear()
    assert await poll(gh, store) == []
    assert gh.requests == ["GET /search/issues"]  # unchanged PR costs no per-PR call


async def test_new_commit_renotifies_but_comment_only_update_does_not(gh, tmp_path):
    store = Store(tmp_path / "s.sqlite3")
    gh.add(1)
    await poll(gh, store)
    gh.prs[1]["updated"] = "t2"  # someone commented: updated_at moves, head does not
    assert await poll(gh, store) == []
    gh.prs[1].update(updated="t3", sha="bbb")  # a push
    again = await poll(gh, store)
    assert [(p["number"], p["why"]) for p in again] == [(1, "new commits")]
    assert await poll(gh, store) == []


async def test_pr_that_leaves_and_returns_is_announced_again(gh, tmp_path):
    store = Store(tmp_path / "s.sqlite3")
    gh.add(1)
    await poll(gh, store)
    del gh.prs[1]  # went back to draft, merged or closed
    assert await poll(gh, store) == []
    gh.add(1)
    assert [p["why"] for p in await poll(gh, store)] == ["new"]


async def test_only_own_repos_and_burst_batches_into_one_notice(gh, tmp_path):
    store = Store(tmp_path / "s.sqlite3")
    gh.add(1, repo="someorg/client")
    for n in range(2, 2 + MAX_LISTED + 3):
        gh.add(n, title=f"PR {n}")
    prs = await poll(gh, store)
    assert all(p["repo"] == f"{USER}/demo" for p in prs) and len(prs) == MAX_LISTED + 3
    text = notice_text(prs)
    assert text.count("•") == MAX_LISTED and "and 3 more" in text and text.startswith(f"*{len(prs)} PRs ready")
    assert notice_key(prs) == notice_key(list(reversed(prs)))  # stable, so a retried send is deduped


async def test_old_list_state_is_ignored_not_crashing(gh, tmp_path):
    store = Store(tmp_path / "s.sqlite3")
    store.kv_set("main", "github_seen", ["github:pr:x/y#1"])
    gh.add(1)
    assert len(await poll(gh, store)) == 1


def test_titles_cannot_ping_or_inject_markup():
    from aoa.slack_bridge import to_mrkdwn
    # escaped, not stripped: no mention, no masked link, no broken code span
    assert slack_safe("<!channel> [click](http://evil) `x`") == "&lt;!channel&gt; [click] (http://evil) 'x'"
    assert "<http://evil|" not in to_mrkdwn(slack_safe("[click](http://evil)"))
    for s, want in (("tool_choice", "tool_choice"), ("a & b", "a &amp; b"), ("<!here>", "&lt;!here&gt;"),
                    ('pip install -e ".[dev]"', 'pip install -e ".[dev]"')):
        assert slack_safe(s) == want
    assert slack_safe("[WIP] x] y", label=True) == "(WIP) x) y"
    pr = {"repo": f"{USER}/demo", "number": 3, "title": "<!channel> [WIP] fix tool_choice", "author": "o",
          "url": "https://github.com/x/pull/3", "why": "new"}
    out = to_mrkdwn(notice_text([pr]))
    assert "<!channel>" not in out and "&lt;!channel&gt;" in out
    assert "<https://github.com/x/pull/3|" in out and "(WIP) fix tool_choice>" in out  # the link survives


async def test_bad_token_is_a_clear_error(gh, tmp_path):
    gh.status = 401
    with pytest.raises(GithubError, match="GITHUB_TOKEN"):
        await poll(gh, Store(tmp_path / "s.sqlite3"))
