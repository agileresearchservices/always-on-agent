import time

from aoa.pricing import cost_usd
from aoa.store import Store
from aoa.usage_report import collect, kind_of, render


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
