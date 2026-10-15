"""Flagging bad outcomes from stored traces (#54): no server, scripted model."""
from __future__ import annotations

import dataclasses
import json
import sys

from aoa import flag
from aoa.direct import handle_events
from aoa.testing import text, tool
from conftest import usr_event


async def test_report_issue_records_one_flag_and_dedupes(make_core):
    call = tool("tool_search", query="mistake")
    core, _ = make_core([
        [call],
        [tool("call_deferred", name="report_issue", arguments={"kind": "wrong_answer", "summary": "I gave the wrong date"})],
        [tool("call_deferred", name="report_issue", arguments={"kind": "wrong_answer", "summary": "I gave the wrong date"})],
        text("noted"),
    ])
    await handle_events(core, "main", [usr_event("what day is it")])
    flags = core.store.flags_list()
    assert len(flags) == 1
    assert flags[0]["conv_id"] == "main"
    assert flags[0]["reason"].startswith("reported:wrong_answer:")
    assert flags[0]["evidence"].startswith("[model-reported, unverified]")


async def test_report_issue_rejects_unknown_kind_and_caps_open_reports(make_core):
    core, _ = make_core([])
    try:
        flag.report(core.store, "main", "bogus", "x")
    except ValueError as e:
        assert "kind must be one of" in str(e)
    else:
        raise AssertionError("expected ValueError")
    for i in range(flag.REPORTED_OPEN_CAP):
        assert flag.report(core.store, "main", "other", f"issue {i}") == "recorded"
    assert flag.report(core.store, "main", "other", "one more").startswith("not recorded")
    assert len(core.store.flags_list()) == flag.REPORTED_OPEN_CAP


async def test_scan_flags_denied_approval_once(make_core, settings):
    core, _ = make_core([])
    core.store.approval_request("ap1", "main", {"tool": "external_send", "channel": "email:bob"})
    core.store.approval_resolve("ap1", approved=False)
    core.store.approval_request("ap2", "main", {"tool": "external_send"})
    core.store.approval_resolve("ap2", approved=True)
    assert [f["reason"] for f in flag.scan(core.store, settings)] == ["approval_denied:ap1"]
    assert flag.scan(core.store, settings) == []  # idempotent
    assert len(core.store.flags_list()) == 1


async def test_scan_flags_repeated_tool_errors_and_max_tokens(make_core, settings):
    bad = lambda: tool("read_file", path="missing.txt")  # noqa: E731
    core, _ = make_core([[bad()], [bad()], [bad()], text("giving up")])
    await handle_events(core, "main", [usr_event("read it")])
    reasons = [f["reason"] for f in flag.scan(core.store, settings)]
    assert len(reasons) == 1 and reasons[0].startswith("tool_errors:read_file:")

    core2, _ = make_core([])
    core2.store.step_put("c2", "llm:t.s0", {"content": [], "stop_reason": "max_tokens", "usage": {}, "tool_uses": [],
                                             "text": "", "cache_miss": False})
    assert [f["reason"] for f in flag.scan(core2.store, settings)] == ["max_tokens:llm:t.s0"]


async def test_scan_flags_a_turn_that_used_every_step(make_core, settings):
    s = dataclasses.replace(settings, max_steps_per_turn=3)
    core, _ = make_core([])
    for n in range(3):
        core.store.step_put("main", f"llm:abc.s{n}", {"content": [], "stop_reason": "tool_use", "usage": {},
                                                       "tool_uses": [{"id": f"t{n}", "name": "bash", "input": {}}],
                                                       "text": "", "cache_miss": False})
    got = flag.scan(core.store, s)
    assert [f["reason"] for f in got] == ["max_steps:abc"]


async def test_scan_flags_cache_miss_and_ignores_healthy_traces(make_core, settings):
    core, _ = make_core([text("hi there")])
    await handle_events(core, "main", [usr_event("hello")])
    assert flag.scan(core.store, settings) == []
    core.store.step_put("main", "llm:zz.s0", {"content": [], "stop_reason": "end_turn", "usage": {},
                                              "tool_uses": [], "text": "x", "cache_miss": True})
    core.store.step_put("main", "llm:zz.s1", {"content": [], "stop_reason": "end_turn", "usage": {},
                                              "tool_uses": [], "text": "x", "cache_miss": True})
    got = flag.scan(core.store, settings)
    assert [f["reason"] for f in got] == ["cache_miss"] and "2 time(s)" in got[0]["evidence"]  # one flag per conversation


async def test_render_trace_text_and_json(make_core):
    core, _ = make_core([text("hello back")])
    await handle_events(core, "main", [usr_event("hello")])
    out = flag.render_trace(core.store, "main")
    assert "conversation main" in out and "hello back" in out and "user:" in out
    data = json.loads(flag.render_trace(core.store, "main", as_json=True))
    assert data["conv"] == "main" and data["messages"][-1]["role"] == "assistant"
    assert "no messages" in flag.render_trace(core.store, "nope")


def test_cli_flags_and_trace(settings, make_core, capsys, monkeypatch):
    import asyncio

    core, _ = make_core([text("ok")])
    asyncio.run(handle_events(core, "main", [usr_event("hello")]))
    core.store.flag_add("main", "reported:other:abcd1234", "[model-reported, unverified] x")
    from aoa.__main__ import main

    monkeypatch.setattr(sys, "argv", ["aoa", "flags", "--scan"])
    main()
    assert "reported:other:abcd1234" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["aoa", "trace", "main", "--json"])
    main()
    assert json.loads(capsys.readouterr().out)["conv"] == "main"
