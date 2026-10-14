"""GitHub monitor: tell the person about every open, non-draft PR in their repos, once per head commit.

A Temporal Schedule fires GithubPollWorkflow every few minutes; its activity calls `poll_once`, which
costs one GitHub search call (plus one small call per PR that changed) and NO model call. The notice goes
straight to the Slack outbox, so PR titles never reach the model. The token lives in the worker's
environment, not in the sandbox.
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Any

import httpx

from .store import Store

API = "https://api.github.com"
MAX_PRS = 50  # open PRs inspected per poll
MAX_LISTED = 15  # PRs named in one notice; the rest are counted
SEEN_KEY = "github_seen"
MAX_REVIEWS_PER_POLL = 3
QUEUE_KEY = "github_review_queue"


class GithubError(RuntimeError):
    pass


def token_provider() -> str:
    """Where the GitHub credential comes from: the worker's environment. The single seam to change if the
    credential source ever does."""
    tok = os.environ.get("GITHUB_TOKEN")
    if not tok:
        raise GithubError("GITHUB_TOKEN is not set")
    return tok


def _clean(s: str, limit: int = 200) -> str:
    return re.sub(r"\s+", " ", s or "").strip()[:limit]


def slack_safe(s: str) -> str:
    """Untrusted text going into a Slack message: no mentions, links or markup."""
    return re.sub(r"[<>&\[\]`*_~|]", "", s)


def _headers(token: str, accept: str = "application/vnd.github+json") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}


def _raise_for(r: httpx.Response) -> None:
    if r.status_code in (401, 403):
        raise GithubError(f"GitHub rejected the token ({r.status_code}): check GITHUB_TOKEN and its expiry/permissions")
    r.raise_for_status()


async def fetch_ready_prs(http: httpx.AsyncClient, token: str, user: str) -> list[dict[str, Any]]:
    """Open, non-draft PRs in repos owned by `user`."""
    r = await http.get(
        f"{API}/search/issues",
        params={"q": f"is:pr is:open draft:false archived:false user:{user}", "per_page": MAX_PRS,
                "sort": "created", "order": "asc"},
        headers=_headers(token),
    )
    _raise_for(r)
    prs = []
    for it in r.json().get("items", []):
        repo = it["repository_url"].removeprefix(f"{API}/repos/")
        if repo.split("/")[0].lower() != user.lower():
            continue  # scope: only the user's own repos, never org/client repos they can see
        prs.append({
            "repo": repo, "number": it["number"], "title": _clean(it.get("title", "")),
            "author": _clean((it.get("user") or {}).get("login", ""), 60), "url": it["html_url"],
            "updated": it.get("updated_at", ""),
        })
    return prs


async def head_sha(http: httpx.AsyncClient, token: str, pr: dict[str, Any]) -> str:
    r = await http.get(f"{API}/repos/{pr['repo']}/pulls/{pr['number']}", headers=_headers(token))
    _raise_for(r)
    return r.json()["head"]["sha"]


def pr_key(pr: dict[str, Any]) -> str:
    return f"{pr['repo']}#{pr['number']}"


async def poll_once(http: httpx.AsyncClient, store: Store, session: str, user: str, token: str) -> list[dict[str, Any]]:
    """PRs to announce now: new ones, and ones whose head commit changed. Remembers what it returns."""
    prs = await fetch_ready_prs(http, token, user)
    old = store.kv_get(session, SEEN_KEY, {})
    if isinstance(old, list):  # state from the earlier review-requested monitor
        old = {}
    seen: dict[str, dict[str, str]] = {}
    announce = []
    for pr in prs:
        key, prev = pr_key(pr), old.get(pr_key(pr))
        if prev and prev["updated"] == pr["updated"]:
            seen[key] = prev  # nothing happened since last poll; no extra call
            continue
        sha = await head_sha(http, token, pr)
        seen[key] = {"sha": sha, "updated": pr["updated"]}
        if prev is None:
            announce.append({**pr, "sha": sha, "why": "new"})
        elif prev["sha"] != sha:
            announce.append({**pr, "sha": sha, "why": "new commits"})
    # PRs that closed, merged or went back to draft drop out, so a later re-ready is announced again
    store.kv_set(session, SEEN_KEY, seen)
    return announce


def reviews_to_start(store: Store, session: str, prs: list[dict[str, Any]], user: str,
                     cap: int = MAX_REVIEWS_PER_POLL) -> list[dict[str, Any]]:
    """Only the owner's own PRs are auto-reviewed: others' PR content is attacker-controllable, and the
    review is published. At most `cap` per poll to bound cost; the rest wait in a persisted queue (the
    seen-state already counts them as announced, so they would otherwise never be reviewed)."""
    queue = store.kv_get(session, QUEUE_KEY, [])
    for p in prs:
        if p["author"].lower() == user.lower():
            queue = [q for q in queue if pr_key(q) != pr_key(p)] + [p]  # a newer commit replaces an older one
    take, rest = queue[:cap], queue[cap:]
    store.kv_set(session, QUEUE_KEY, rest)
    return take


def notice_text(prs: list[dict[str, Any]]) -> str:
    n = len(prs)
    lines = [f"*{n} PR{'s' if n != 1 else ''} ready for review*"]
    for pr in prs[:MAX_LISTED]:
        tail = " (new commits)" if pr["why"] == "new commits" else ""
        lines.append(f"• [{slack_safe(pr['repo'])}#{pr['number']} {slack_safe(pr['title'])}]({pr['url']}) "
                     f"by {slack_safe(pr['author'])}{tail}")
    if n > MAX_LISTED:
        lines.append(f"…and {n - MAX_LISTED} more")
    return "\n".join(lines)


def notice_key(prs: list[dict[str, Any]]) -> str:
    return "ghpr:" + hashlib.sha1("|".join(sorted(f"{pr_key(p)}@{p['sha']}" for p in prs)).encode()).hexdigest()[:16]


# ---- agent tools (github_read / github_write) ---------------------------------------------------
def owner_user() -> str:
    u = os.environ.get("AOA_GITHUB_USER")
    if not u:
        raise GithubError("AOA_GITHUB_USER is not set")
    return u


def check_repo_in_scope(repo: str) -> str:
    """Only repos owned by the configured user; org/client repos the token can see are off limits."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repo or ""):
        raise GithubError(f"'{repo}' is not an owner/name repo")
    if repo.split("/")[0].lower() != owner_user().lower():
        raise GithubError(f"{repo} is outside the allowed scope (only {owner_user()}/* repos)")
    return repo


async def gh_call(method: str, path: str, json: Any = None, accept: str = "application/vnd.github+json") -> httpx.Response:
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.request(
            method, f"{API}{path}", json=json,
            headers={"Authorization": f"Bearer {token_provider()}", "Accept": accept,
                     "X-GitHub-Api-Version": "2022-11-28"},
        )
    if r.status_code in (401, 403, 404):
        raise GithubError(f"GitHub returned {r.status_code} for {method} {path}: check the repo name and token permissions")
    r.raise_for_status()
    return r
