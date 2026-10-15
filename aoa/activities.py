"""Temporal activities: thin wrappers over AgentCore, one per durable step."""
from __future__ import annotations

import secrets
import time
import uuid
from datetime import timedelta
from typing import Any
from urllib.parse import quote

import httpx
from temporalio import activity
from temporalio.client import Client

from .config import Settings
from .core import AgentCore
from .delivery import deliver_event
from .events import Event
from . import github_monitor as gh
from . import search_attrs
from .gh_reviewer import forget as forget_reviewer_token, reviewer_token
from .github_monitor import notice_key, notice_text, poll_once, reviews_to_start, token_provider
from .pr_merge import OFFER_FOR_KEY, OFFER_KEY, RESOLVED_KEY, RETRYABLE, check_summary, decide, report_text
from .pr_resolve import AUTO_MERGE_ON, ResolveError, Resolver, resolve_root, with_heartbeat
from .tools import read_github
from .pr_review import (
    REVIEW_READ_CHARS, check_citations, diff_index, github_body, load_focus, parse_verdict, slack_text,
)


class AgentActivities:
    def __init__(self, core: AgentCore, client: Client, settings: Settings):
        self.core, self.client, self.s = core, client, settings

    @activity.defn(name="prepare_turn")
    async def prepare_turn(self, a: dict[str, Any]) -> dict[str, Any]:
        return await self.core.prepare_turn(a["session"], a["conv"], a["turn_key"], a["events"])

    @activity.defn(name="prepare_helper")
    async def prepare_helper(self, a: dict[str, Any]) -> None:
        await self.core.prepare_helper(a["conv"], a["task"])

    @activity.defn(name="llm_step")
    async def llm_step(self, a: dict[str, Any]) -> dict[str, Any]:
        return await self.core.llm_step(a["session"], a["conv"], a["step_key"], a["role"], a["depth"])

    @activity.defn(name="run_tool")
    async def run_tool(self, a: dict[str, Any]) -> dict[str, Any]:
        return await self.core.run_tool(
            a["session"], a["conv"], a["role"], a["depth"], a["tool_use"], a["trigger_user"]
        )

    @activity.defn(name="append_results")
    async def append_results(self, a: dict[str, Any]) -> None:
        await self.core.append_results(a["session"], a["conv"], a["step_key"], a["results"], a["events"])

    @activity.defn(name="maybe_compact")
    async def maybe_compact(self, a: dict[str, Any]) -> str:
        return await self.core.maybe_compact(a["session"], a["conv"])

    @activity.defn(name="approvals_next")
    async def approvals_next(self, session: str) -> float | None:
        return await self.core.approvals_next(session)

    @activity.defn(name="approvals_tick")
    async def approvals_tick(self, session: str) -> dict[str, Any]:
        return await self.core.approvals_tick(session)

    @activity.defn(name="digest_status")
    async def digest_status(self, session: str) -> float | None:
        return await self.core.digest_status(session)

    @activity.defn(name="flush_digest")
    async def flush_digest(self, session: str) -> dict[str, Any]:
        return await self.core.flush_digest(session)

    @activity.defn(name="sandbox_sleep")
    async def sandbox_sleep(self, session: str) -> str:
        return await self.core.sandbox_sleep(session)

    @activity.defn(name="warm_cache")
    async def warm_cache(self, a: dict[str, Any]) -> dict[str, int]:
        return await self.core.warm_cache(a["session"], a["conv"])

    @activity.defn(name="report_failure")
    async def report_failure(self, a: dict[str, Any]) -> None:
        self.core.store.outbox_push(a["session"], "system", {"text": a["text"]}, client_key=a.get("client_key"))

    @activity.defn(name="deliver_routine_event")
    async def deliver_routine_event(self, a: dict[str, Any]) -> None:
        ev = Event(
            source="routine", kind="routine", priority=1, trusted=True,
            text=f"[Routine '{a['name']}' fired]\n{a['prompt']}",
            dedupe_key=a["dedupe_key"],
        )
        await deliver_event(self.client, self.s, a["session"], ev.to_dict())

    @activity.defn(name="poll_github")
    async def poll_github(self, a: dict[str, Any]) -> dict[str, Any]:
        """Announce PRs ready for review straight to the outbox (no agent turn, no model call), and return
        the ones the workflow should start a review for."""
        async with httpx.AsyncClient(timeout=20) as http:
            prs = await poll_once(http, self.core.store, a["session"], a["user"], token_provider())
        if prs:
            self.core.store.outbox_push(a["session"], "update", {"text": notice_text(prs)}, client_key=notice_key(prs))
        review = reviews_to_start(self.core.store, a["session"], prs, a["user"], settle=self.s.review_settle_seconds)
        for pr in review:  # a commit aoa wrote to resolve conflicts: point the reviewer at those files
            files = self.core.store.kv_get(a["session"], f"{RESOLVED_KEY}{pr['repo']}#{pr['number']}@{pr['sha']}")
            if files is not None:
                pr["resolved_files"] = files
            if focus := load_focus(self.s.data_dir, pr["repo"]):
                pr["review_focus"] = focus
        return {"announced": len(prs), "review": review, "search_attrs": search_attrs.registered()}

    @activity.defn(name="fetch_pr_for_review")
    async def fetch_pr_for_review(self, a: dict[str, Any]) -> str:
        """The PR (untrusted-wrapped, up to REVIEW_READ_CHARS) for the reviewer's first message. Empty on any
        failure: the reviewer then reads the PR itself with the github_read tool, as it did before this existed."""
        pr = a["pr"]
        try:
            return await read_github("pr", pr["repo"], pr["number"], REVIEW_READ_CHARS)
        except Exception as e:  # noqa: BLE001 - the fallback path covers every failure
            activity.logger.warning("fetch_pr_for_review failed for %s#%s, the reviewer will page through the PR itself: %s: %s",
                                    pr.get("repo"), pr.get("number"), type(e).__name__, e)
            return ""

    @activity.defn(name="publish_pr_review")
    async def publish_pr_review(self, a: dict[str, Any]) -> dict[str, Any]:
        """Validate the reviewer's answer, post it on GitHub pinned to the reviewed commit (a real APPROVE /
        REQUEST_CHANGES when a reviewer identity is configured, else a comment review), and put a card in Slack. Idempotent: the card has a client key, and a repeat post is skipped."""
        pr, session = a["pr"], a["session"]
        v = parse_verdict(a.get("review_text") or "")
        if v is not None:
            v = await self._check_citations(pr, v)
        posted, note, posted_as = False, a.get("note", ""), "a comment review"
        done_key = f"prreview-posted:{pr['repo']}#{pr['number']}@{pr['sha']}"
        url = f"/repos/{pr['repo']}/pulls/{pr['number']}/reviews"
        if v is not None:
            if done := self.core.store.kv_get(session, done_key):
                posted = True
                posted_as = done if isinstance(done, str) else posted_as
            elif tok := await reviewer_token(pr["repo"]):  # #11: a real APPROVE / REQUEST_CHANGES
                try:
                    await gh.gh_call("POST", url, {"body": github_body(v, formal=True), "commit_id": pr["sha"],
                                                   "event": "APPROVE" if v["verdict"] == "approve" else "REQUEST_CHANGES"},
                                     token=tok)
                    posted_as = "an approval" if v["verdict"] == "approve" else "a change request"
                    self.core.store.kv_set(session, done_key, posted_as)
                    posted = True
                except (gh.GithubError, httpx.HTTPStatusError) as e:  # never lose the review: comment instead
                    if isinstance(e, httpx.HTTPStatusError) and e.response.status_code >= 500:
                        raise
                    forget_reviewer_token(pr["repo"])
                    code = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else str(e)[:120]
                    note = f"The reviewer identity could not post ({code}), so this is a comment review. {note}".strip()
            if not posted:
                try:
                    await gh.gh_call("POST", url, {"body": github_body(v), "event": "COMMENT", "commit_id": pr["sha"]})
                    self.core.store.kv_set(session, done_key, True)
                    posted = True
                except gh.GithubError as e:  # 401/403/404: retrying will not help
                    note = f"GitHub post failed: {str(e)[:200]}"
                except httpx.HTTPStatusError as e:
                    if e.response.status_code >= 500:
                        raise  # transient: let Temporal retry the activity
                    note = f"GitHub post failed: {e.response.status_code} {e.response.text[:150]}"
                # other httpx errors (network) propagate and are retried too
        card = slack_text(v, pr, posted, note, posted_as)
        payload = {"text": card, "repo": pr["repo"], "number": pr["number"], "sha": pr["sha"],
                   "url": pr["url"], "verdict": v["verdict"] if v else None}
        if v is not None and v["verdict"] == "approve":
            payload["merge_offer"] = self._merge_offer(session, pr, card)
        self.core.store.outbox_push(
            session, "pr_review", payload, client_key=f"prreview:{pr['repo']}#{pr['number']}@{pr['sha']}",
        )
        return {"verdict": v["verdict"] if v else None, "posted": posted}

    async def _check_citations(self, pr: dict[str, Any], v: dict[str, Any]) -> dict[str, Any]:
        """Remove locations the PR's diff cannot support (the findings stay). Skipped (the review is used as it
        is) when the diff is unavailable, too large for GitHub, or the head moved since the review: this check
        must never stop a review from being published."""
        path = f"/repos/{pr['repo']}/pulls/{pr['number']}"
        try:
            if (await gh.gh_call("GET", path)).json()["head"]["sha"] != pr["sha"]:
                return v
            diff = (await gh.gh_call("GET", path, accept="application/vnd.github.diff")).text
            return check_citations(v, diff_index(diff)) if diff.strip() else v
        except Exception:  # noqa: BLE001 - an unexpected reply shape or network error must not fail the publish
            return v

    def _merge_offer(self, session: str, pr: dict[str, Any], card: str) -> str:
        """An opaque id the [Merge] button carries. It names this PR at this reviewed commit, so a click can
        only ever merge what was reviewed. Stable across activity retries."""
        st = self.core.store
        for_key = f"{OFFER_FOR_KEY}{pr['repo']}#{pr['number']}@{pr['sha']}"
        oid = st.kv_get(session, for_key)
        if not oid:
            oid = "mrg-" + secrets.token_hex(6)
            st.kv_set(session, f"{OFFER_KEY}{oid}", {
                "repo": pr["repo"], "number": pr["number"], "sha": pr["sha"], "url": pr["url"],
                "title": pr.get("title", ""), "verdict": "approve", "card_text": card,
            })
            st.kv_set(session, for_key, oid)
        return oid

    # ---- merge (MergePrWorkflow); started only by a human click, never by the model ------------------
    @activity.defn(name="merge_inspect")
    async def merge_inspect(self, a: dict[str, Any]) -> dict[str, Any]:
        """GitHub's current view of the PR, reduced to one decision. Read-only, so safe to retry."""
        repo, n, sha = gh.check_repo_in_scope(a["repo"]), a["number"], a["sha"]
        pr = (await gh.gh_call("GET", f"/repos/{repo}/pulls/{n}")).json()
        failing, pending = [], []
        if not pr.get("merged") and pr.get("state") == "open":
            status = (await gh.gh_call("GET", f"/repos/{repo}/commits/{sha}/status")).json()
            runs = (await gh.gh_call("GET", f"/repos/{repo}/commits/{sha}/check-runs?per_page=100")).json()
            failing, pending = check_summary(status, runs)
        action, reason = decide(pr, sha, failing, pending)
        base = pr.get("base") or {}
        head = pr.get("head") or {}
        return {
            "action": action, "reason": reason, "title": pr.get("title", ""),
            "head_ref": head.get("ref", ""),
            "protected_refs": [r for r in (base.get("ref"), (base.get("repo") or {}).get("default_branch")) if r],
            "base_ref": base.get("ref", ""),
            "same_repo": ((head.get("repo") or {}).get("full_name") or "").lower() == repo.lower(),
            "default_base": base.get("ref") == (base.get("repo") or {}).get("default_branch"),
            "merge_sha": pr.get("merge_commit_sha") if pr.get("merged") else None,
            # read before merging: what `Closes #N` will close (None = could not tell)
            "issues": await self._closing_issues(repo, n) if action in ("merge", "already_merged") else None,
        }

    async def _closing_issues(self, repo: str, n: int) -> list[dict[str, Any]] | None:
        owner, name = repo.split("/")
        q = ("query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n){"
             "closingIssuesReferences(first:20){nodes{number state repository{nameWithOwner}}}}}}")
        try:
            data = (await gh.gh_call("POST", "/graphql", {"query": q, "variables": {"o": owner, "r": name, "n": n}})).json()
            nodes = data["data"]["repository"]["pullRequest"]["closingIssuesReferences"]["nodes"]
        except (gh.GithubError, httpx.HTTPStatusError, KeyError, TypeError):
            return None
        return [{"repo": x["repository"]["nameWithOwner"], "number": x["number"], "state": x["state"]} for x in nodes]

    @activity.defn(name="merge_pr")
    async def merge_pr(self, a: dict[str, Any]) -> dict[str, Any]:
        """Squash-merge pinned to the reviewed commit. Safe to retry: GitHub's state is checked first, and a
        refused or failed merge is re-checked, since a merge can succeed and still return an error."""
        repo, n, sha = gh.check_repo_in_scope(a["repo"]), a["number"], a["sha"]

        async def merged() -> dict[str, Any] | None:
            pr = (await gh.gh_call("GET", f"/repos/{repo}/pulls/{n}")).json()
            return {"merged": True, "merge_sha": pr.get("merge_commit_sha") or ""} if pr.get("merged") else None

        if done := await merged():
            return done
        try:
            r = await gh.gh_call("PUT", f"/repos/{repo}/pulls/{n}/merge", {
                "merge_method": "squash", "sha": sha, "commit_title": f"{a['title']} (#{n})"[:250],
            })
            return {"merged": True, "merge_sha": r.json().get("sha", "")}
        except httpx.HTTPStatusError as e:
            if done := await merged():
                return done
            if e.response.status_code >= 500:
                raise  # transient; the retry re-checks first
            try:
                msg = str(e.response.json().get("message", ""))
            except ValueError:
                msg = ""
            return {"merged": False, "reason": f"GitHub refused the merge ({e.response.status_code}): {msg[:200]}"}
        except gh.GithubError as e:
            return {"merged": False, "reason": str(e)[:200]}

    @activity.defn(name="merge_update_branch")
    async def merge_update_branch(self, a: dict[str, Any]) -> dict[str, Any]:
        repo, n = gh.check_repo_in_scope(a["repo"]), a["number"]
        try:
            await gh.gh_call("PUT", f"/repos/{repo}/pulls/{n}/update-branch", {"expected_head_sha": a["sha"]})
            return {"ok": True}
        except httpx.HTTPStatusError as e:
            if e.response.status_code >= 500:
                raise
            return {"ok": False, "reason": f"GitHub refused to update the branch ({e.response.status_code})"}
        except gh.GithubError as e:
            return {"ok": False, "reason": str(e)[:200]}

    @activity.defn(name="merge_delete_branch")
    async def merge_delete_branch(self, a: dict[str, Any]) -> dict[str, Any]:
        repo, ref = gh.check_repo_in_scope(a["repo"]), a["ref"]
        if not a["same_repo"] or not ref:
            return {"ref": ref, "result": "left alone (it lives in a fork)"}
        if ref in a.get("protected_refs", []):
            return {"ref": ref, "result": "left alone (it is the base or default branch)"}
        try:
            await gh.gh_call("DELETE", f"/repos/{repo}/git/refs/heads/{quote(ref, safe='/')}")
            return {"ref": ref, "result": "deleted"}
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 422:  # already gone (e.g. the repo auto-deletes merged branches)
                return {"ref": ref, "result": "deleted"}
            if e.response.status_code >= 500:
                raise
            return {"ref": ref, "result": f"not deleted ({e.response.status_code})"}
        except gh.GithubError:  # 404: already gone, or no permission
            return {"ref": ref, "result": "not found (already deleted?)"}

    @activity.defn(name="merge_issue_states")
    async def merge_issue_states(self, issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for i in issues:
            try:
                repo = gh.check_repo_in_scope(i["repo"])
                st = (await gh.gh_call("GET", f"/repos/{repo}/issues/{i['number']}")).json().get("state", "")
                out.append({**i, "state": st.upper()})
            except (gh.GithubError, httpx.HTTPStatusError):
                out.append({**i, "state": "UNKNOWN"})
        return out

    @activity.defn(name="resolve_conflicts")
    async def resolve_conflicts(self, a: dict[str, Any]) -> dict[str, Any]:
        """Merge the base into the reviewed commit in a throwaway clone, let a model resolve the conflicted
        files, run the repo's tests and push (no force). Long-running: heartbeats while it works."""
        repo = gh.check_repo_in_scope(a["repo"])
        if not a["same_repo"] or a["head_ref"] in a.get("protected_refs", []):
            return {"status": "failed", "action": "needs_human",
                    "reason": "the PR comes from a fork or its head is a base/default branch; resolve the conflicts by hand"}

        async def auto_merge_off() -> None:  # with auto-merge on, the push alone could merge a model-written commit
            pr = (await gh.gh_call("GET", f"/repos/{repo}/pulls/{a['number']}")).json()
            if pr.get("auto_merge"):
                raise ResolveError(AUTO_MERGE_ON, "auto_merge")

        try:
            await auto_merge_off()
        except ResolveError as e:
            return {"status": "failed", "action": e.action, "reason": str(e)}
        workdir = resolve_root() / uuid.uuid4().hex[:12]
        limit = activity.info().start_to_close_timeout or timedelta(minutes=30)
        deadline = time.monotonic() + limit.total_seconds() - 60
        mark = lambda sha, files: self.core.store.kv_set(  # noqa: E731
            a["session"], f"{RESOLVED_KEY}{repo}#{a['number']}@{sha}", files)
        r = Resolver(repo=repo, sha=a["sha"], head_ref=a["head_ref"], base_ref=a["base_ref"], workdir=workdir,
                     remote_url=a.get("remote_url") or f"https://github.com/{repo}.git", token=token_provider(),
                     llm=self.core.llm, model=self.s.helper_model, max_tokens=self.s.max_tokens,
                     protected_refs=tuple(a.get("protected_refs", [])),
                     on_usage=lambda m, u: self.core.store.usage_add(f"resolve/{repo}#{a['number']}", m, u),
                     on_commit=mark, before_push=auto_merge_off, deadline=deadline)
        out = await with_heartbeat(r.run(), activity.heartbeat)
        if out["status"] == "resolved":
            mark(out["new_sha"], out["files"])  # also on a retry that found its earlier push
        return out

    @activity.defn(name="merge_report")
    async def merge_report(self, a: dict[str, Any]) -> None:
        """Final notice, plus what the bridge needs to update the original card."""
        offer, out = a["offer"], a["outcome"]
        self.core.store.outbox_push(
            a["session"], "pr_merge",
            {"text": report_text(offer, out), "status": out["status"], "offer_id": a["offer_id"],
             "card_ts": a.get("card_ts"), "card_text": offer.get("card_text", ""),
             "retry": out["status"] == "failed" and out.get("action") in RETRYABLE},
            client_key=f"prmerge:{a['run_id']}",
        )

    def all(self) -> list:
        return [
            self.prepare_turn, self.prepare_helper, self.llm_step, self.run_tool, self.append_results,
            self.maybe_compact, self.digest_status, self.flush_digest, self.approvals_next, self.approvals_tick, self.sandbox_sleep, self.warm_cache, self.report_failure,
            self.deliver_routine_event, self.poll_github, self.fetch_pr_for_review, self.publish_pr_review,
            self.merge_inspect, self.merge_pr, self.merge_update_branch, self.merge_delete_branch,
            self.merge_issue_states, self.merge_report, self.resolve_conflicts,
        ]
