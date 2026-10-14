"""Slack bridge logic with a fake Slack client and a mock aoa server (no network)."""
import asyncio

import httpx
import pytest

from aoa.slack_bridge import (
    Bridge, BridgeConfig, classify_inbound, parse_block_action, parse_merge_action, render_outbox_item, to_mrkdwn,
)

CFG = BridgeConfig(channel="C1", user="U1")


def msg(**kw):
    return {"type": "message", "channel": "C1", "user": "U1", "text": "hi", "ts": "1.1", **kw}


def test_classify_accepts_only_the_allowed_user_in_the_channel():
    assert classify_inbound(msg(), CFG) == ("message", {"text": "hi", "message_id": "slack:C1:1.1"})
    assert classify_inbound(msg(user="U2"), CFG) is None
    assert classify_inbound(msg(channel="C2"), CFG) is None
    assert classify_inbound(msg(bot_id="B1"), CFG) is None
    assert classify_inbound(msg(subtype="message_changed"), CFG) is None
    assert classify_inbound(msg(text="  "), CFG) is None
    assert classify_inbound({"type": "reaction_added"}, CFG) is None


def test_typed_decisions():
    assert classify_inbound(msg(text="approve apr-90096ede"), CFG) == ("decision", ("apr-90096ede", True))
    assert classify_inbound(msg(text="Deny apr-1a"), CFG) == ("decision", ("apr-1a", False))
    assert classify_inbound(msg(text="approve everything"), CFG)[0] == "message"


def action(user="U1", channel="C1", action_id="aoa_approve", value="apr-1"):
    return {"type": "block_actions", "user": {"id": user}, "channel": {"id": channel},
            "actions": [{"action_id": action_id, "value": value}]}


def test_block_actions_are_restricted_to_the_user_and_channel():
    assert parse_block_action(action(), CFG) == ("apr-1", True)
    assert parse_block_action(action(action_id="aoa_deny"), CFG) == ("apr-1", False)
    assert parse_block_action(action(user="U2"), CFG) is None
    assert parse_block_action(action(channel="C2"), CFG) is None
    assert parse_block_action(action(action_id="other"), CFG) is None


def test_rendering():
    assert to_mrkdwn("**bold** [a](https://x.io)\n# Head") == "*bold* <https://x.io|a>\n*Head*"
    text, blocks = render_outbox_item({"kind": "reply", "payload": {"text": "ok"}})
    assert (text, blocks) == ("ok", None)
    text, blocks = render_outbox_item({"kind": "approval", "payload": {"text": "Send?", "approval_id": "apr-1"}})
    assert [b["value"] for b in blocks[1]["elements"]] == ["apr-1", "apr-1"]
    assert render_outbox_item({"kind": "urgent", "payload": {"text": "x"}})[0].startswith(":rotating_light:")
    assert render_outbox_item({"kind": "alert", "payload": {"text": "x"}})[0].startswith(":warning: *ALERT*")


class FakeWeb:
    def __init__(self):
        self.posted, self.updated = [], []

    async def chat_postMessage(self, **kw):
        self.posted.append(kw)
        return {"ts": f"t{len(self.posted)}"}

    async def chat_update(self, **kw):
        self.updated.append(kw)


def make_bridge(tmp_path, handler):
    http = httpx.AsyncClient(base_url="http://aoa", transport=httpx.MockTransport(handler))
    web = FakeWeb()
    return Bridge(CFG, web, http, tmp_path / "cursor.txt"), web


async def test_message_is_posted_to_the_server_with_a_stable_id(tmp_path):
    seen = []

    def handler(req):
        seen.append((req.url.path, req.content))
        return httpx.Response(202, json={"accepted": True})

    bridge, _ = make_bridge(tmp_path, handler)
    await bridge.handle_event(msg())
    await bridge.handle_event(msg(user="U2"))
    assert len(seen) == 1 and seen[0][0] == "/sessions/main/messages"
    assert b"slack:C1:1.1" in seen[0][1]


async def test_button_click_resolves_approval_and_removes_buttons(tmp_path):
    seen = []

    def handler(req):
        seen.append((req.url.path, req.content))
        return httpx.Response(200, json={"status": "approved"})

    bridge, web = make_bridge(tmp_path, handler)
    payload = action() | {"message": {"ts": "9.9", "text": "Send?"}}
    await bridge.handle_interactive(payload)
    assert seen == [("/sessions/main/approvals/apr-1", b'{"approve":true}')]
    assert web.updated[0]["ts"] == "9.9" and "blocks" in web.updated[0]


async def test_outbox_pump_starts_from_now_then_posts_new_items(tmp_path):
    state = {"calls": 0}

    async def handler(req):
        state["calls"] += 1
        after = int(req.url.params["after"])
        items = [{"seq": 1, "kind": "reply", "payload": {"text": "old"}},
                 {"seq": 2, "kind": "reply", "payload": {"text": "old2"}}]
        if state["calls"] == 1:  # first-run probe: history exists but must not be replayed
            return httpx.Response(200, json={"items": items})
        if after == 2 and "sent" not in state:
            state["sent"] = True
            return httpx.Response(200, json={"items": [{"seq": 3, "kind": "reply", "payload": {"text": "new"}}]})
        await asyncio.sleep(0.05)  # a real long poll blocks until `wait` expires
        return httpx.Response(200, json={"items": []})

    bridge, web = make_bridge(tmp_path, handler)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bridge.pump_outbox(), timeout=0.5)
    assert [p["text"] for p in web.posted] == ["new"]
    assert (tmp_path / "cursor.txt").read_text() == "3"


async def test_duplicate_click_is_applied_once(tmp_path):
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200 if len(calls) == 1 else 409, json={"status": "approved"})

    bridge, web = make_bridge(tmp_path, handler)
    payload = action() | {"message": {"ts": "9.9", "text": "Send?"}}
    await asyncio.gather(bridge.handle_interactive(payload), bridge.handle_interactive(payload))
    assert calls == ["/sessions/main/approvals/apr-1"]
    assert not web.posted  # no "already decided" noise


async def test_typed_decision_updates_the_remembered_card(tmp_path):
    def handler(req):
        return httpx.Response(200, json={"status": "approved"})

    bridge, web = make_bridge(tmp_path, handler)
    bridge._cards["apr-1"] = ("5.5", "Send?")
    await bridge.handle_event(msg(text="deny apr-1"))
    assert web.updated[0]["ts"] == "5.5" and "Denied" in str(web.updated[0]["blocks"])
    assert "apr-1" not in bridge._cards


async def test_pump_remembers_the_card_it_posts(tmp_path):
    state = {"calls": 0}

    async def handler(req):
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(200, json={"items": []})
        if state["calls"] == 2:
            item = {"seq": 1, "kind": "approval", "payload": {"text": "Send?", "approval_id": "apr-9"}}
            return httpx.Response(200, json={"items": [item]})
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"items": []})

    bridge, web = make_bridge(tmp_path, handler)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bridge.pump_outbox(), timeout=0.4)
    assert bridge._cards["apr-9"][0] == "t1"


# ---- Merge button (PR flow stage 2) -------------------------------------------------------------
def test_only_approved_review_cards_get_a_merge_button():
    _, blocks = render_outbox_item({"kind": "pr_review", "payload": {"text": "ok", "merge_offer": "mrg-1"}})
    btn = blocks[-1]["elements"][0]
    assert btn["action_id"] == "aoa_merge" and btn["value"] == "mrg-1" and "confirm" in btn
    assert render_outbox_item({"kind": "pr_review", "payload": {"text": "bad"}})[1] is None


def test_merge_clicks_are_restricted_to_the_user_and_channel():
    assert parse_merge_action(action(action_id="aoa_merge", value="mrg-1"), CFG) == "mrg-1"
    assert parse_merge_action(action(action_id="aoa_merge", user="U2"), CFG) is None
    assert parse_merge_action(action(action_id="aoa_merge", channel="C2"), CFG) is None
    assert parse_merge_action(action(), CFG) is None  # an approval click is not a merge


async def test_merge_click_starts_the_merge_once_and_removes_the_button(tmp_path):
    seen = []

    def handler(req):
        seen.append((req.url.path, req.content))
        return httpx.Response(202, json={"status": "started"})

    bridge, web = make_bridge(tmp_path, handler)
    payload = action(action_id="aoa_merge", value="mrg-1") | {"message": {"ts": "9.9", "text": "card"}}
    await asyncio.gather(bridge.handle_interactive(payload), bridge.handle_interactive(payload))
    assert seen == [("/sessions/main/merges/mrg-1", b'{"card_ts":"9.9"}')]
    blocks = web.updated[0]["blocks"]
    assert web.updated[0]["ts"] == "9.9" and "Merging" in str(blocks) and "aoa_merge" not in str(blocks)


async def test_merge_click_that_the_server_rejects_says_so(tmp_path):
    bridge, web = make_bridge(tmp_path, lambda req: httpx.Response(404, json={"detail": "no such merge offer"}))
    await bridge.handle_interactive(action(action_id="aoa_merge", value="mrg-1") | {"message": {"ts": "9.9"}})
    assert "Could not start the merge" in web.posted[0]["text"] and not web.updated
    assert "mrg-1" not in bridge._merging  # a later click may try again


async def test_merge_click_network_error_does_not_jam_the_button(tmp_path):
    def handler(req):
        raise httpx.ConnectError("down")

    bridge, _ = make_bridge(tmp_path, handler)
    with pytest.raises(httpx.ConnectError):
        await bridge.handle_interactive(action(action_id="aoa_merge", value="mrg-1") | {"message": {"ts": "9.9"}})
    assert "mrg-1" not in bridge._merging


async def test_merge_result_posts_notice_and_updates_the_card(tmp_path):
    state = {"calls": 0}
    results = [
        {"seq": 1, "kind": "pr_merge", "payload": {"text": ":tada: *Merged*", "status": "merged", "offer_id": "mrg-1",
                                                   "card_ts": "9.9", "card_text": "card", "retry": False}},
        {"seq": 2, "kind": "pr_merge", "payload": {"text": "Not merged", "status": "failed", "offer_id": "mrg-2",
                                                   "card_ts": "8.8", "card_text": "card2", "retry": True}},
    ]

    async def handler(req):
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(200, json={"items": []})
        if state["calls"] == 2:
            return httpx.Response(200, json={"items": results})
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"items": []})

    bridge, web = make_bridge(tmp_path, handler)
    bridge._merging |= {"mrg-1", "mrg-2"}
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bridge.pump_outbox(), timeout=0.4)
    assert [p["text"] for p in web.posted] == [":tada: *Merged*", "Not merged"]
    merged, failed = web.updated
    assert merged["ts"] == "9.9" and "Merged" in str(merged["blocks"]) and "aoa_merge" not in str(merged["blocks"])
    assert failed["ts"] == "8.8" and "aoa_merge" in str(failed["blocks"])  # retry offered on the same card
    assert not bridge._merging
