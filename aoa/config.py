from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    model: str
    helper_model: str
    review_model: str
    temporal_address: str
    temporal_namespace: str
    task_queue: str
    tz: str
    # agent loop
    max_steps_per_turn: int
    max_helper_steps: int
    max_helper_depth: int
    max_tokens: int
    tool_result_max_chars: int
    # context / cache
    context_hard_limit_tokens: int
    context_warm_compact_tokens: int
    cache_ttl_seconds: int
    # lifecycle
    idle_sleep_seconds: int
    batch_window_seconds: float
    # notifications
    min_update_interval_seconds: int
    quiet_start_hour: int
    quiet_end_hour: int
    # sandbox
    shell_timeout_seconds: int
    approval_nudge_seconds: int
    approval_expire_seconds: int

    @classmethod
    def from_env(cls) -> "Settings":
        sonnet = os.environ.get("AOA_MODEL", "claude-sonnet-5-5")
        return cls(
            data_dir=Path(os.environ.get("AOA_DATA_DIR", "./data")).resolve(),
            model=sonnet,
            helper_model=os.environ.get("AOA_HELPER_MODEL", sonnet),
            review_model=os.environ.get("AOA_REVIEW_MODEL", "claude-haiku-5-5"),
            temporal_address=os.environ.get("AOA_TEMPORAL_ADDRESS", "localhost:7233"),
            temporal_namespace=os.environ.get("AOA_TEMPORAL_NAMESPACE", "default"),
            task_queue=os.environ.get("AOA_TASK_QUEUE", "aoa"),
            tz=os.environ.get("AOA_TZ", "America/New_York"),
            max_steps_per_turn=_int("AOA_MAX_STEPS", 40),
            max_helper_steps=_int("AOA_MAX_HELPER_STEPS", 30),
            max_helper_depth=_int("AOA_MAX_HELPER_DEPTH", 1),
            max_tokens=_int("AOA_MAX_TOKENS", 16000),
            tool_result_max_chars=_int("AOA_TOOL_RESULT_MAX_CHARS", 8000),
            context_hard_limit_tokens=_int("AOA_CONTEXT_HARD_LIMIT", 120_000),
            context_warm_compact_tokens=_int("AOA_CONTEXT_WARM_COMPACT", 50_000),
            cache_ttl_seconds=_int("AOA_CACHE_TTL", 300),
            idle_sleep_seconds=_int("AOA_IDLE_SLEEP", 600),
            batch_window_seconds=_float("AOA_BATCH_WINDOW", 2.0),
            min_update_interval_seconds=_int("AOA_MIN_UPDATE_INTERVAL", 1800),
            quiet_start_hour=_int("AOA_QUIET_START", 22),
            quiet_end_hour=_int("AOA_QUIET_END", 7),
            shell_timeout_seconds=_int("AOA_SHELL_TIMEOUT", 60),
            approval_nudge_seconds=_int("AOA_APPROVAL_NUDGE", 4 * 3600),
            approval_expire_seconds=_int("AOA_APPROVAL_EXPIRE", 24 * 3600),
        )

    # Derived paths -------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "aoa.sqlite3"

    @property
    def memory_dir(self) -> Path:
        return self.data_dir / "memory"

    @property
    def sandbox_dir(self) -> Path:
        return self.data_dir / "sandbox"
