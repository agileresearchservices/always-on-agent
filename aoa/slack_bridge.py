"""Slack client for the agent: a bridge process, like client_cli, that only speaks the HTTP API.

    python -m aoa.slack_bridge            # needs the slack extra: pip install -e '.[slack]'

Inbound  (Socket Mode, so no public URL): messages from ONE allowed user in ONE private channel become
         trusted user messages; button clicks and "approve <id>" / "deny <id>" resolve approvals.
Outbound: long-polls the session outbox and posts each item to the channel, with Approve/Deny buttons
         for approval requests and a Merge button on approved PR review cards. A merge result also
         updates the card it came from.

Everything else in the workspace is ignored. See docs/slack-setup.md for creating the app.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("aoa.slack")

PREFIX = {
    "reply": "",
    "update": "_update_ ",
    "question": ":question: ",
    "alert": ":warning: *ALERT* ",
    "urgent": ":rotating_light: *URGENT* ",
    "approval": ":lock: ",
    "system": "_system_ ",
    "sent_log": "_outbound_ ",
    "pr_review": ":mag: ",
    "pr_merge": "",
    "eval_proposal": ":test_tube: ",
    "eval_fix": ":hammer_and_wrench: ",
}
TYPED_DECISION = re.compile(r"^\s*(approve|deny)\s+(apr-[0-9a-f]+)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class BridgeConfig:
    channel: str  # private channel ID (C... or G...)
    user: str  # the only Slack user ID allowed to talk to the agent (U...)
    session: str = "main"


def to_mrkdwn(text: str) -> str:
    """Convert the common Markdown the model writes into Slack mrkdwn."""
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    return re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)


def classify_inbound(event: dict[str, Any], cfg: BridgeConfig) -> tuple[str, Any] | None:
    """Decide what a Slack message event means. None = ignore it (wrong place, wrong person, a bot)."""
    if event.get("type") != "message" or event.get("channel") != cfg.channel:
        return None
    if event.get("user") != cfg.user or event.get("bot_id") or event.get("subtype"):
        return None  # only the person; edits, joins, bot echoes and file shares etc. are skipped
    text = (event.get("text") or "").strip()
    if not text:
        return None
    m = TYPED_DECISION.match(text)
    if m:
        return "decision", (m.group(2), m.group(1).lower() == "approve")
    return "message", {"text": text, "message_id": f"slack:{event['channel']}:{event['ts']}"}


def parse_block_action(payload: dict[str, Any], cfg: BridgeConfig) -> tuple[str, bool] | None:
    """Approve/Deny button click -> (approval_id, approved), only from the allowed user in our channel."""
    if payload.get("type") != "block_actions":
        return None
    if (payload.get("user") or {}).get("id") != cfg.user:
        return None
    if (payload.get("channel") or {}).get("id") != cfg.channel:
        return None
    for act in payload.get("actions", []):
        if act.get("action_id") in ("aoa_approve", "aoa_deny") and act.get("value"):
            return act["value"], act["action_id"] == "aoa_approve"
    return None


def parse_merge_action(payload: dict[str, Any], cfg: BridgeConfig) -> str | None:
    """Merge button click -> merge offer id, only from the allowed user in our channel."""
    if payload.get("type") != "block_actions" or (payload.get("user") or {}).get("id") != cfg.user:
        return None
    if (payload.get("channel") or {}).get("id") != cfg.channel:
        return None
    for act in payload.get("actions", []):
        if act.get("action_id") == "aoa_merge" and act.get("value"):
            return act["value"]
    return None


def parse_proposal_action(payload: dict[str, Any], cfg: BridgeConfig) -> tuple[str, bool] | None:
    """Accept/Reject click on an eval-case card -> (proposal id, accept), only from the allowed user in our channel."""
    if payload.get("type") != "block_actions" or (payload.get("user") or {}).get("id") != cfg.user:
        return None
    if (payload.get("channel") or {}).get("id") != cfg.channel:
        return None
    for act in payload.get("actions", []):
        if act.get("action_id") in ("aoa_case_accept", "aoa_case_reject") and act.get("value"):
            return act["value"], act["action_id"] == "aoa_case_accept"
    return None


def parse_fix_action(payload: dict[str, Any], cfg: BridgeConfig) -> str | None:
    """[Draft fix] click on an accepted eval-case card -> proposal id, only from the allowed user in our channel."""
    if payload.get("type") != "block_actions" or (payload.get("user") or {}).get("id") != cfg.user:
        return None
    if (payload.get("channel") or {}).get("id") != cfg.channel:
        return None
    for act in payload.get("actions", []):
        if act.get("action_id") == "aoa_case_fix" and act.get("value"):
            return act["value"]
    return None


def parse_fix_open_action(payload: dict[str, Any], cfg: BridgeConfig) -> tuple[str, str] | None:
    """[Run checks + open PR] click -> (proposal id, attempt token); the button value is "<proposal id>:<token>"."""
    if payload.get("type") != "block_actions" or (payload.get("user") or {}).get("id") != cfg.user:
        return None
    if (payload.get("channel") or {}).get("id") != cfg.channel:
        return None
    for act in payload.get("actions", []):
        if act.get("action_id") == "aoa_fix_open" and ":" in (act.get("value") or ""):
            pid, token = act["value"].split(":", 1)
            return pid, token
    return None


def open_pr_button(pid: str, token: str) -> dict[str, Any]:
    return {"type": "actions", "block_id": f"aoa_fixopen_{pid}", "elements": [
        {"type": "button", "action_id": "aoa_fix_open", "style": "primary", "value": f"{pid}:{token}",
         "text": {"type": "plain_text", "text": "Run checks + open PR"},
         "confirm": {"title": {"type": "plain_text", "text": "Have you read the diff?"},
                     "text": {"type": "mrkdwn", "text": "Runs the fix against the real model with the API key (costs a little), then, only if it passes, pushes the branch and opens a PR. aoa will review it; merging stays your call."},
                     "confirm": {"type": "plain_text", "text": "Run and open"}, "deny": {"type": "plain_text", "text": "Cancel"}}},
    ]}


def fix_button(pid: str) -> dict[str, Any]:
    return {"type": "actions", "block_id": f"aoa_fix_{pid}", "elements": [
        {"type": "button", "action_id": "aoa_case_fix", "value": pid, "text": {"type": "plain_text", "text": "Draft fix"},
         "confirm": {"title": {"type": "plain_text", "text": "Draft a fix PR?"},
                     "text": {"type": "mrkdwn", "text": "Runs a coding session (up to its dollar cap) and real-model eval runs, "
                                                        "then opens a PR only if every check passes. It never merges."},
                     "confirm": {"type": "plain_text", "text": "Draft fix"}, "deny": {"type": "plain_text", "text": "Cancel"}}},
    ]}


def proposal_blocks(text: str, pid: str | None = None, status: str | None = None, fix_for: str | None = None) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [{"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}}]
    if status:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": status}]})
    if fix_for:
        blocks.append(fix_button(fix_for))
    if pid:
        blocks.append({"type": "actions", "block_id": f"aoa_{pid}", "elements": [
            {"type": "button", "action_id": "aoa_case_accept", "style": "primary", "value": pid,
             "text": {"type": "plain_text", "text": "Accept case"}},
            {"type": "button", "action_id": "aoa_case_reject", "style": "danger", "value": pid,
             "text": {"type": "plain_text", "text": "Reject"}},
        ]})
    return blocks


def merge_button(offer_id: str) -> dict[str, Any]:
    return {"type": "actions", "block_id": f"aoa_{offer_id}", "elements": [
        {"type": "button", "action_id": "aoa_merge", "style": "primary", "value": offer_id,
         "text": {"type": "plain_text", "text": "Merge"},
         "confirm": {"title": {"type": "plain_text", "text": "Squash-merge this PR?"},
                     "text": {"type": "mrkdwn", "text": "Squash-merges the reviewed commit and deletes the branch."},
                     "confirm": {"type": "plain_text", "text": "Merge"},
                     "deny": {"type": "plain_text", "text": "Cancel"}}},
    ]}


def card_blocks(card_text: str, status: str | None = None, offer_id: str | None = None) -> list[dict[str, Any]]:
    """A PR review card: its text, an optional status line, and a Merge button when one is offered."""
    blocks: list[dict[str, Any]] = [{"type": "section", "text": {"type": "mrkdwn", "text": card_text[:2900]}}]
    if status:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": status}]})
    if offer_id:
        blocks.append(merge_button(offer_id))
    return blocks


MERGE_STATUS = {"merged": ":tada: Merged", "updated": ":arrows_counterclockwise: Branch updated; re-review coming",
                "resolved": ":wrench: Conflicts resolved and pushed; re-review coming",
                "failed": ":no_entry: Not merged (see below)"}


def render_outbox_item(item: dict[str, Any]) -> tuple[str, list[dict[str, Any]] | None]:
    """Outbox row -> (fallback text, Block Kit blocks or None)."""
    kind, p = item["kind"], item["payload"]
    text = PREFIX.get(kind, f"_{kind}_ ") + to_mrkdwn(str(p.get("text", "")))
    if kind == "eval_fix" and p.get("proposal_id") and (p.get("ready") or p.get("retry")):
        btn = open_pr_button(p["proposal_id"], p.get("token") or "") if p.get("ready") else fix_button(p["proposal_id"])
        return text, [{"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}}, btn]
    if kind == "eval_proposal" and p.get("proposal_id"):
        return text, proposal_blocks(text, p["proposal_id"])
    if kind == "pr_review" and p.get("merge_offer"):
        return text, card_blocks(text, offer_id=p["merge_offer"])
    if kind != "approval" or not p.get("approval_id"):
        return text, None
    aid = p["approval_id"]
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {"type": "actions", "block_id": f"aoa_{aid}", "elements": [
            {"type": "button", "action_id": "aoa_approve", "style": "primary", "value": aid,
             "text": {"type": "plain_text", "text": "Approve"}},
            {"type": "button", "action_id": "aoa_deny", "style": "danger", "value": aid,
             "text": {"type": "plain_text", "text": "Deny"}},
        ]},
    ]
    return text, blocks


def resolved_blocks(original_text: str, approved: bool) -> list[dict[str, Any]]:
    verdict = ":white_check_mark: Approved" if approved else ":no_entry: Denied"
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": original_text}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": verdict}]},
    ]


class Bridge:
    def __init__(self, cfg: BridgeConfig, web: Any, http: httpx.AsyncClient, cursor_path: Path):
        self.cfg, self.web, self.http, self.cursor_path = cfg, web, http, cursor_path
        self._decided: set[str] = set()  # Slack can deliver one click twice; act on each approval once
        self._cards: dict[str, tuple[str, str]] = {}  # approval_id -> (message ts, text) of its posted card
        self._deciding: set[Any] = set()  # eval-case clicks in flight (Slack may deliver twice)
        self._merging: set[str] = set()  # merge offers clicked and not yet reported (Slack may deliver twice)

    # ---- Slack -> agent --------------------------------------------------------------
    async def handle_event(self, event: dict[str, Any]) -> None:
        kind = classify_inbound(event, self.cfg)
        if kind is None:
            return
        what, data = kind
        if what == "decision":
            await self._decide(*data)
        else:
            r = await self.http.post(f"/sessions/{self.cfg.session}/messages", json=data)
            self._check(r, "send message")

    async def handle_interactive(self, payload: dict[str, Any]) -> None:
        offer = parse_merge_action(payload, self.cfg)
        if offer is not None:
            msg = payload.get("message") or {}
            await self._merge(offer, msg.get("ts"), msg.get("text", ""))
            return
        opened = parse_fix_open_action(payload, self.cfg)
        if opened is not None:
            msg = payload.get("message") or {}
            await self._open_fix(opened[0], opened[1], msg.get("ts"), msg.get("text", ""))
            return
        fix = parse_fix_action(payload, self.cfg)
        if fix is not None:
            msg = payload.get("message") or {}
            await self._fix(fix, msg.get("ts"), msg.get("text", ""))
            return
        prop = parse_proposal_action(payload, self.cfg)
        if prop is not None:
            msg = payload.get("message") or {}
            await self._proposal(prop[0], prop[1], msg.get("ts"), msg.get("text", ""))
            return
        parsed = parse_block_action(payload, self.cfg)
        if parsed is None:
            return
        aid, approved = parsed
        msg = payload.get("message") or {}
        await self._decide(aid, approved, card=(msg.get("ts"), msg.get("text", "")))

    async def _decide(self, approval_id: str, approved: bool, card: tuple[str | None, str] | None = None) -> None:
        if approval_id in self._decided:
            return
        self._decided.add(approval_id)
        r = await self.http.post(
            f"/sessions/{self.cfg.session}/approvals/{approval_id}", json={"approve": approved}
        )
        if r.status_code == 409:
            try:
                detail = str(r.json().get("detail") or "")
            except ValueError:
                detail = ""
            what = detail if "expired" in detail else "was already decided"
            await self.web.chat_postMessage(channel=self.cfg.channel, text=f"_{approval_id} {what}._")
            return
        self._check(r, "decide approval")
        if r.status_code >= 400:
            return
        ts, text = self._cards.pop(approval_id, None) or card or (None, "")
        if not ts:
            return  # a card from before this bridge started and a typed reply: nothing to update
        try:  # replace the buttons, whether the decision was clicked or typed
            await self.web.chat_update(
                channel=self.cfg.channel, ts=ts, text=text, blocks=resolved_blocks(text, approved),
            )
        except Exception:  # noqa: BLE001 - cosmetic only
            log.warning("could not update approval message", exc_info=True)

    async def _merge(self, offer_id: str, ts: str | None, text: str) -> None:
        if offer_id in self._merging:
            return
        self._merging.add(offer_id)
        try:
            r = await self.http.post(f"/sessions/{self.cfg.session}/merges/{offer_id}", json={"card_ts": ts})
        except Exception:
            self._merging.discard(offer_id)  # never leave the button dead until a restart
            raise
        if r.status_code == 409:
            return  # already running; its result will arrive
        if r.status_code >= 400:
            self._merging.discard(offer_id)
            self._check(r, "start merge")
            await self.web.chat_postMessage(channel=self.cfg.channel, text=f"_Could not start the merge ({r.status_code})._")
            return
        if ts:
            try:  # take the button away while the merge runs
                await self.web.chat_update(channel=self.cfg.channel, ts=ts, text=text,
                                           blocks=card_blocks(text, ":hourglass_flowing_sand: Merging…"))
            except Exception:  # noqa: BLE001 - cosmetic only
                log.warning("could not update PR card", exc_info=True)

    async def _proposal(self, pid: str, accept: bool, ts: str | None, text: str) -> None:
        if pid in self._deciding:
            return
        self._deciding.add(pid)  # kept after success: Slack can deliver one click twice
        try:
            r = await self.http.post(f"/sessions/{self.cfg.session}/proposals/{pid}", json={"accept": accept})
        except httpx.HTTPError:
            self._deciding.discard(pid)
            await self.web.chat_postMessage(channel=self.cfg.channel, text=f"_{pid}: could not reach the aoa server; click again._")
            return
        if r.status_code >= 400:
            self._deciding.discard(pid)
            self._check(r, "decide proposal")
            try:
                detail = str(r.json().get("detail") or "")
            except ValueError:
                detail = ""
            await self.web.chat_postMessage(channel=self.cfg.channel, text=f"_{pid}: {detail or r.status_code}_")
            return
        out = r.json()
        status = (f":white_check_mark: Accepted, committed to local branch `{out.get('branch')}`" if accept
                  else ":no_entry: Rejected")
        if ts:
            try:
                await self.web.chat_update(channel=self.cfg.channel, ts=ts, text=text,
                                           blocks=proposal_blocks(text, None, status, fix_for=pid if accept else None))
            except Exception:  # noqa: BLE001 - cosmetic only
                log.warning("could not update eval-case card", exc_info=True)

    async def _fix(self, pid: str, ts: str | None, text: str) -> None:
        if ("fix", pid) in self._deciding:
            return
        self._deciding.add(("fix", pid))
        try:
            r = await self.http.post(f"/sessions/{self.cfg.session}/proposals/{pid}/fix")
        except httpx.HTTPError:
            self._deciding.discard(("fix", pid))
            await self.web.chat_postMessage(channel=self.cfg.channel, text=f"_{pid}: could not reach the aoa server; click again._")
            return
        if r.status_code >= 400:
            self._deciding.discard(("fix", pid))
            self._check(r, "start fix")
            try:
                detail = str(r.json().get("detail") or "")
            except ValueError:
                detail = ""
            await self.web.chat_postMessage(channel=self.cfg.channel, text=f"_{pid}: {detail or r.status_code}_")
            return
        if ts:
            try:  # take the button away while it runs; the result arrives as its own message
                await self.web.chat_update(channel=self.cfg.channel, ts=ts, text=text, blocks=proposal_blocks(
                    text, None, ":hourglass_flowing_sand: Drafting a fix; the result will be posted here"))
            except Exception:  # noqa: BLE001 - cosmetic only
                log.warning("could not update eval-case card", exc_info=True)

    async def _open_fix(self, pid: str, token: str, ts: str | None, text: str) -> None:
        key = ("open", pid)
        if key in self._deciding:
            return
        self._deciding.add(key)
        try:
            r = await self.http.post(f"/sessions/{self.cfg.session}/proposals/{pid}/fix/open", json={"token": token})
        except httpx.HTTPError:
            self._deciding.discard(key)
            await self.web.chat_postMessage(channel=self.cfg.channel, text=f"_{pid}: could not reach the aoa server; click again._")
            return
        if r.status_code >= 400:
            self._deciding.discard(key)
            self._check(r, "open fix PR")
            try:
                detail = str(r.json().get("detail") or "")
            except ValueError:
                detail = ""
            await self.web.chat_postMessage(channel=self.cfg.channel, text=f"_{pid}: {detail or r.status_code}_")
            return
        if ts:
            try:
                await self.web.chat_update(channel=self.cfg.channel, ts=ts, text=text, blocks=proposal_blocks(
                    text, None, ":hourglass_flowing_sand: Running the real-model checks, then opening the PR; the result will be posted here"))
            except Exception:  # noqa: BLE001 - cosmetic only
                log.warning("could not update fix card", exc_info=True)

    async def _merge_result(self, p: dict[str, Any]) -> None:
        """A merge finished: update the card it started from (button back only if a retry can help)."""
        self._merging.discard(p.get("offer_id", ""))
        if not p.get("card_ts"):
            return
        card = PREFIX["pr_review"] + to_mrkdwn(str(p.get("card_text", "")))
        blocks = card_blocks(card, MERGE_STATUS.get(p.get("status"), p.get("status")),
                             p.get("offer_id") if p.get("retry") else None)
        try:
            await self.web.chat_update(channel=self.cfg.channel, ts=p["card_ts"], text=card, blocks=blocks)
        except Exception:  # noqa: BLE001 - cosmetic only
            log.warning("could not update PR card", exc_info=True)

    def _check(self, r: httpx.Response, what: str) -> None:
        if r.status_code == 401:
            raise RuntimeError("aoa server rejected the bridge (401): check AOA_API_TOKEN")
        if r.status_code >= 400:
            log.error("%s failed: %s %s", what, r.status_code, r.text[:200])

    # ---- agent -> Slack ----------------------------------------------------------------
    def _load_cursor(self) -> int | None:
        try:
            return int(self.cursor_path.read_text().strip())
        except (OSError, ValueError):
            return None

    def _save_cursor(self, seq: int) -> None:
        self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
        self.cursor_path.write_text(str(seq))

    async def pump_outbox(self) -> None:
        sid = self.cfg.session
        after = self._load_cursor()
        if after is None:  # first run: start from now, do not replay history into Slack
            r = await self.http.get(f"/sessions/{sid}/outbox", params={"after": 0, "wait": 0})
            self._check(r, "read outbox")
            items = r.json().get("items", [])
            after = items[-1]["seq"] if items else 0
            self._save_cursor(after)
        while True:
            try:
                r = await self.http.get(f"/sessions/{sid}/outbox", params={"after": after, "wait": 20})
                self._check(r, "read outbox")
                for item in r.json().get("items", []):
                    text, blocks = render_outbox_item(item)
                    kw: dict[str, Any] = {"channel": self.cfg.channel, "text": text,
                                      "unfurl_links": False, "unfurl_media": False}  # GitHub preview cards repeat the line
                    if blocks:
                        kw["blocks"] = blocks
                    resp = await self.web.chat_postMessage(**kw)
                    aid = item["payload"].get("approval_id") if item["kind"] == "approval" else None
                    ts = resp.get("ts") if resp is not None and hasattr(resp, "get") else None
                    if aid and ts:
                        self._cards[aid] = (ts, text)
                    if item["kind"] == "pr_merge":
                        await self._merge_result(item["payload"])
                    if item["kind"] == "eval_fix":  # a result arrived: its buttons may be clicked again
                        pid = item["payload"].get("proposal_id")
                        self._deciding.discard(("fix", pid)), self._deciding.discard(("open", pid))
                    after = item["seq"]
                    self._save_cursor(after)  # after posting: a crash re-sends at most one item
            except httpx.HTTPError:
                await asyncio.sleep(2)
            except RuntimeError:
                raise
            except Exception:  # noqa: BLE001 - Slack API hiccup: keep the cursor, retry
                log.exception("failed to post to Slack; retrying")
                await asyncio.sleep(5)


def _require(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f"{name} is not set (see .env.example and docs/slack-setup.md)")
    return v


async def run() -> None:
    from slack_sdk.socket_mode.aiohttp import SocketModeClient
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.socket_mode.response import SocketModeResponse
    from slack_sdk.web.async_client import AsyncWebClient

    cfg = BridgeConfig(
        channel=_require("SLACK_CHANNEL_ID"), user=_require("SLACK_ALLOWED_USER_ID"),
        session=os.environ.get("AOA_SESSION", "main"),
    )
    token = os.environ.get("AOA_API_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    data_dir = Path(os.environ.get("AOA_DATA_DIR", "./data")).resolve()
    web = AsyncWebClient(token=_require("SLACK_BOT_TOKEN"))
    async with httpx.AsyncClient(
        base_url=os.environ.get("AOA_SERVER", "http://127.0.0.1:8000"), headers=headers, timeout=70
    ) as http:
        bridge = Bridge(cfg, web, http, data_dir / f"slack_cursor_{cfg.session}.txt")
        sm = SocketModeClient(app_token=_require("SLACK_APP_TOKEN"), web_client=web)

        async def on_request(client: SocketModeClient, req: SocketModeRequest) -> None:
            await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
            try:
                if req.type == "events_api":
                    await bridge.handle_event(req.payload.get("event", {}))
                elif req.type == "interactive":
                    await bridge.handle_interactive(req.payload)
            except Exception:  # noqa: BLE001
                log.exception("error handling Slack %s", req.type)

        sm.socket_mode_request_listeners.append(on_request)
        await sm.connect()
        log.info("slack bridge up: channel %s, user %s, session %s", cfg.channel, cfg.user, cfg.session)
        await bridge.pump_outbox()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())


if __name__ == "__main__":
    main()
