from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from aoa.config import Settings
from aoa.core import AgentCore
from aoa.events import Event, user_event
from aoa.memory import MemoryStore
from aoa.store import Store
from aoa.testing import FakeLLM, MemoryScheduler

AFTERNOON = datetime(2026, 10, 14, 14, 0, tzinfo=ZoneInfo("America/New_York"))


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("AOA_DATA_DIR", str(tmp_path))
    return Settings.from_env()


@pytest.fixture
def make_core(settings):
    def _make(script, clock=AFTERNOON):
        store = Store(settings.db_path)
        memory = MemoryStore(settings.memory_dir)
        llm = FakeLLM(script)
        core = AgentCore(settings, store, memory, llm, MemoryScheduler(), clock=lambda: clock)
        return core, llm

    return _make


def bg_event(text="hello", source="webhook", key=None) -> dict:
    return Event(source=source, text=text, dedupe_key=key or f"{source}:{text}").to_dict()


def usr_event(text="hi", key=None) -> dict:
    return user_event(text, key).to_dict()
