"""Notifier decisions are one transaction: concurrent sends, send vs flush, and failures stay consistent."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from aoa.notify import Notifier
from aoa.store import Store

ET = ZoneInfo("America/New_York")
NOON = datetime(2026, 10, 14, 12, 0, tzinfo=ET)


def _run_threads(fns):
    errors = []

    def wrap(f):
        def run():
            try:
                f()
            except BaseException as e:  # surfaced below
                errors.append(e)
        return run

    ts = [threading.Thread(target=wrap(f)) for f in fns]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=60)
    assert not errors, errors


def test_concurrent_sends_within_interval_send_exactly_one(settings):
    store = Store(settings.db_path)
    n = Notifier(store, settings)
    results = []
    barrier = threading.Barrier(2)

    def go(text):
        def f():
            barrier.wait()
            results.append(n.send("s", "update", text, NOON)["status"])
        return f

    _run_threads([go("a"), go("b")])
    assert sorted(results) == ["held", "sent"]
    assert len(store.outbox_since("s")) == 1
    assert n.digest_count("s") == 1


def test_append_interleaved_with_flush_never_loses_an_entry(settings):
    store = Store(settings.db_path)
    n = Notifier(store, settings)
    n.send("s", "update", "first", NOON)
    for i in range(5):
        n.send("s", "update", f"held{i}", NOON + timedelta(minutes=1))
    later = NOON + timedelta(hours=2)
    barrier = threading.Barrier(2)

    def flush():
        barrier.wait()
        n.flush_digest("s", later)

    def append():
        barrier.wait()
        # may be held, or go out carrying the digest, depending on order; the text must land somewhere
        n.send("s", "update", "late", later)

    _run_threads([flush, append])
    where = " ".join(r["payload"]["text"] for r in store.outbox_since("s")) + " ".join(
        store.kv_get("s", "digest", [])
    )
    assert "late" in where
    for i in range(5):
        assert f"held{i}" in where


def test_failure_after_outbox_insert_rolls_everything_back(settings, monkeypatch):
    store = Store(settings.db_path)
    n = Notifier(store, settings)
    n.send("s", "update", "one", NOON)
    n.send("s", "update", "two", NOON + timedelta(minutes=1))  # held
    ts_before = store.kv_get("s", "last_notified_ts")
    rows_before = store.outbox_since("s")
    later = NOON + timedelta(hours=2)

    real = Store.outbox_push_c

    def boom(*a, **k):
        real(*a, **k)
        raise RuntimeError("injected")

    monkeypatch.setattr(Store, "outbox_push_c", staticmethod(boom))
    with pytest.raises(RuntimeError):
        n.send("s", "update", "three", later)
    with pytest.raises(RuntimeError):
        n.flush_digest("s", later)
    monkeypatch.undo()
    assert store.outbox_since("s") == rows_before
    assert store.kv_get("s", "digest") == ["two"]
    assert store.kv_get("s", "last_notified_ts") == ts_before


def test_duplicate_client_key_does_not_abort_the_transaction(settings):
    store = Store(settings.db_path)
    store.outbox_push("s", "reply", {"text": "x"}, client_key="k")
    with store._tx() as c:
        assert Store.outbox_push_c(c, "s", "reply", {"text": "x"}, client_key="k") is None
        Store.kv_set_c(c, "s", "after", 1)  # tx still usable and commits
    assert store.kv_get("s", "after") == 1

    n = Notifier(store, settings)
    store.kv_set("s", "digest", ["held"])
    assert n.send("s", "reply", "x", NOON, client_key="k")["status"] == "duplicate"
    assert store.kv_get("s", "digest") == []  # digest clear committed with the transaction


def test_kv_update_is_atomic(settings):
    store = Store(settings.db_path)

    def bump():
        for _ in range(20):
            store.kv_update("s", "n", lambda v: v + 1, 0)

    _run_threads([bump] * 4)
    assert store.kv_get("s", "n") == 80
