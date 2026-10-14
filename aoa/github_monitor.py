"""GitHub monitor: poll for open PRs that ask for the user's review and wake the agent once per new PR.

A Temporal Schedule fires GithubPollWorkflow every few minutes; its activity calls `poll_once`, which
costs one GitHub search call and NO model call unless there is a PR the agent has not been told about.
Only metadata goes into the event (repo, number, title, author, URL), never the PR body, to keep the
prompt-injection surface small. The token lives in the worker's environment, not in the sandbox.
"""
from __future__ import annotations

import os
import re
from typing import Any

import httpx

from .events import Event
from .store import Store

API = "https://api.github.com"
MAX_NEW_PER_POLL = 10  # a burst (first run, mass review request) is capped; the rest arrive next poll
SEEN_KEY = "github_seen"


class GithubError(RuntimeError):
    pass


def token_provider() -> str:
    """Where the GitHub credential comes from: a read-only fine-grained PAT in the worker's environment.
    This is the single seam to change if the credential source ever does."""
    tok = os.environ.get("GITHUB_TOKEN")
    if not tok:
        raise GithubError("GITHUB_TOKEN is not set")
    return tok


def _clean(s: str, limit: int = 200) -> str:
    return re.sub(r"\s+", " ", s or "").strip()[:limit]


async def fetch_review_requests(http: httpx.AsyncClient, token: str, user: str) -> list[dict[str, Any]]:
    r = await http.get(
        f"{API}/search/issues",
        params={"q": f"is:pr is:open archived:false review-requested:{user} user:{user}", "per_page": 30, "sort": "created",
                "order": "asc"},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"},
    )
    if r.status_code in (401, 403):
        raise GithubError(f"GitHub rejected the token ({r.status_code}): check GITHUB_TOKEN and its expiry/permissions")
    r.raise_for_status()
    prs = []
    for it in r.json().get("items", []):
        repo = it["repository_url"].removeprefix(f"{API}/repos/")
        if repo.split("/")[0].lower() != user.lower():
            continue  # scope: only the user's own repos, never org/client repos they can see
        prs.append({
            "repo": repo, "number": it["number"], "title": _clean(it.get("title", "")),
            "author": _clean((it.get("user") or {}).get("login", ""), 60), "url": it["html_url"],
        })
    return prs


def pr_key(pr: dict[str, Any]) -> str:
    return f"github:pr:{pr['repo']}#{pr['number']}"


def pr_event(pr: dict[str, Any]) -> Event:
    return Event(
        source="github", kind="review_requested", priority=1, trusted=False, dedupe_key=pr_key(pr),
        text=(f"Review requested on a pull request.\nrepo: {pr['repo']}\nPR: #{pr['number']}\n"
              f"title: {pr['title']}\nauthor: {pr['author']}\nurl: {pr['url']}"),
    )


async def poll_once(http: httpx.AsyncClient, store: Store, session: str, user: str, token: str) -> list[Event]:
    """Return events for PRs not announced before, and remember them. Safe to call repeatedly."""
    prs = await fetch_review_requests(http, token, user)
    seen = set(store.kv_get(session, SEEN_KEY, []))
    new = [p for p in prs if pr_key(p) not in seen][:MAX_NEW_PER_POLL]
    current = {pr_key(p) for p in prs}
    # forget PRs no longer awaiting review, so a closed-then-reopened request can be announced again
    store.kv_set(session, SEEN_KEY, sorted((seen & current) | {pr_key(p) for p in new}))
    return [pr_event(p) for p in new]
