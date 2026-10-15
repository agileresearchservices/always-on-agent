import time

from aoa import compaction
from aoa.direct import handle_events
from aoa.pricing import cost_usd
from aoa.safety import autoreview
from aoa.store import Store
from aoa.testing import FakeLLM, text, tool
from aoa.usage_report import collect, kind_of, render

from conftest import usr_event


def test_sonnet_cost_uses_list_prices():
    # 1M of each: 2 + 2.50 (5m write) + 0.10 (read) + 10 (output)
    assert cost_usd("claude-sonnet-5-5", 1_000_000, 1_000_000, 1_000_000, 1_000_000) == 14.6
    assert cost_usd("claude-sonnet-5-5", 0, 0, 0, 1_000_000, cache_ttl_seconds=3600) == 4.0


def test_haiku_long_prompt_tier_counts_cache_tokens():
    short = cost_usd("claude-haiku-5-5", 0, 1_000_000, 50_000, 0)
    long = cost_usd("claude-haiku-5-5", 0, 1_000_000, 150_000, 0)
    assert round(short, 4) == round(0.5 + 0.0005, 4)
    assert round(long, 4) == round(2.5 + 0.0075, 4)


def test_unknown_model_is_unpriced_not_zero():
    assert cost_usd("claude-mystery", 1, 1, 1, 1) is None


def test_kind_of():
    assert kind_of("prreview/o/r#1@abc/helper/review") == "pr-review"
    assert kind_of("main/helper/x") == "helper"
    assert kind_of("main") == "agent"


def test_report_groups_by_day_and_flags_unpriced(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    store.usage_add("main", "claude-sonnet-5-5", {"input_tokens": 0, "output_tokens": 1_000_000})
    store.usage_add("prreview/o/r#1@a/helper/review", "claude-mystery", {"output_tokens": 5})
    text = render(collect(store, time.time() - 3600, 300), 1)
    assert "$10.00" in text and "1 calls unpriced" in text and "average $10.00/day" in text
    assert render([], 2) == "No usage recorded in the last 2 day(s)."


def test_kind_of_side_calls_and_ordering():
    assert kind_of("main#compact") == "compact"
    assert kind_of("main#review") == "cmd-review"
    assert kind_of("main#warm") == "warm"
    assert kind_of("resolve/o/r#3") == "resolve"
    assert kind_of("prreview/o/r#1@a/helper/review") == "pr-review"
    assert kind_of("main/helper/x") == "helper"
    # suffix checks run before the prreview/ and /helper checks
    assert kind_of("prreview/x/helper#review") == "cmd-review"
    assert kind_of("main/helper/x#compact") == "compact"


def test_report_labels_every_kind_and_prints_cache_ttl(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    for conv in ("main", "main#compact", "main#review", "main#warm", "resolve/o/r#3", "prreview/o/r#1@a/helper/review"):
        store.usage_add(conv, "claude-sonnet-5-5", {"output_tokens": 10})
    out = render(collect(store, time.time() - 3600, 300), 1, cache_ttl=300)
    for kind in ("agent", "compact", "cmd-review", "warm", "resolve", "pr-review"):
        assert any(line.strip().startswith(kind + " ") for line in out.splitlines()), kind
    assert "5m rate (AOA_CACHE_TTL=300)" in out
    assert "1h rate (AOA_CACHE_TTL=3600)" in render(collect(store, time.time() - 3600, 3600), 1, cache_ttl=3600)
    assert "AOA_CACHE_TTL" not in render(collect(store, time.time() - 3600, 300), 1)  # kwarg is optional


def _rows(core, conv):
    with core.store._conn() as c:
        return c.execute("SELECT * FROM usage WHERE conv_id=?", (conv,)).fetchall()


async def test_compact_records_spend_and_leaves_main_last_usage_alone(make_core):
    core, _ = make_core([text("the summary")])
    s = core.store
    s.commit_step("main", "u1", {}, role="user", content=[{"type": "text", "text": "one"}])
    s.commit_step("main", "a1", {}, role="assistant", content=[{"type": "text", "text": "ok"}])
    s.commit_step("main", "u2", {}, role="user", content=[{"type": "text", "text": "two"}])
    s.usage_add("main", "claude-sonnet-5-5", {"input_tokens": 7, "output_tokens": 3})
    before = s.last_usage("main")
    system, tools, model = core._setup("main", 0)
    ok = await compaction.compact(llm=core.llm, store=s, conv_id="main", settings=core.s, system=system, tools=tools, model=model)
    assert ok
    assert len(_rows(core, "main#compact")) == 1
    assert s.last_usage("main") == before


async def test_review_spend_recorded_once_per_reviewed_command(make_core):
    core, _ = make_core([
        [tool("run_shell", command="echo hi", why="demo")], text('{"allow": true, "reason": "ok"}'),
        [tool("run_shell", command="echo bye", why="demo")], text('{"allow": false, "reason": "no"}'),
        [tool("run_shell", command="rm -rf /", why="x")],  # denylisted: no reviewer call, no row
        text("done"),
    ])
    await handle_events(core, "main", [usr_event("go")])
    assert len(_rows(core, "main#review")) == 2


async def test_review_usage_callback_failure_cannot_deny():
    def boom(model, usage):
        raise RuntimeError("db locked")

    llm = FakeLLM([text('{"allow": true, "reason": "ok"}')])
    assert (await autoreview(llm, "claude-haiku-5-5", "echo hi", "demo", on_usage=boom)).allow
