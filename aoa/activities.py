"""Temporal activities: thin wrappers over AgentCore, one per durable step."""
from __future__ import annotations

from typing import Any

import httpx
from temporalio import activity
from temporalio.client import Client

from .config import Settings
from .core import AgentCore
from .delivery import deliver_event
from .events import Event
from . import github_monitor as gh
from .github_monitor import notice_key, notice_text, poll_once, reviews_to_start, token_provider
from .pr_review import github_body, parse_verdict, slack_text


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
        return {"announced": len(prs), "review": reviews_to_start(prs, a["user"])}

    @activity.defn(name="publish_pr_review")
    async def publish_pr_review(self, a: dict[str, Any]) -> dict[str, Any]:
        """Validate the reviewer's answer, post it on GitHub as a comment review pinned to the reviewed
        commit, and put a card in Slack. Idempotent: the card has a client key, and a repeat post is skipped."""
        pr, session = a["pr"], a["session"]
        v = parse_verdict(a.get("review_text") or "")
        posted, note = False, a.get("note", "")
        done_key = f"prreview-posted:{pr['repo']}#{pr['number']}@{pr['sha']}"
        if v is not None:
            if self.core.store.kv_get(session, done_key):
                posted = True
            else:
                try:
                    await gh.gh_call("POST", f"/repos/{pr['repo']}/pulls/{pr['number']}/reviews",
                                     {"body": github_body(v), "event": "COMMENT", "commit_id": pr["sha"]})
                    self.core.store.kv_set(session, done_key, True)
                    posted = True
                except (gh.GithubError, httpx.HTTPError) as e:
                    note = f"GitHub post failed: {str(e)[:200]}"
        self.core.store.outbox_push(
            session, "pr_review",
            {"text": slack_text(v, pr, posted, note), "repo": pr["repo"], "number": pr["number"], "sha": pr["sha"],
             "url": pr["url"], "verdict": v["verdict"] if v else None},
            client_key=f"prreview:{pr['repo']}#{pr['number']}@{pr['sha']}",
        )
        return {"verdict": v["verdict"] if v else None, "posted": posted}

    def all(self) -> list:
        return [
            self.prepare_turn, self.prepare_helper, self.llm_step, self.run_tool, self.append_results,
            self.maybe_compact, self.sandbox_sleep, self.warm_cache, self.report_failure,
            self.deliver_routine_event, self.poll_github, self.publish_pr_review,
        ]
