from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path


log = logging.getLogger(__name__)


def _shown(name: str, value: str) -> str:
    """The value as it may appear in a message: never for a secret (*TOKEN, *KEY variable)."""
    return "<hidden>" if name.endswith(("TOKEN", "KEY")) else repr(value)


def _raw(name: str) -> str | None:
    """The variable's text, or None when unset or empty (an empty `AOA_X=` line means unset)."""
    v = os.environ.get(name, "").strip()
    return v or None


def _int(name: str, default: int) -> int:
    raw = _raw(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("%s must be an integer, got %s; using %s", name, _shown(name, raw), default)
        return default


def _float(name: str, default: float) -> float:
    raw = _raw(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("%s must be a number, got %s; using %s", name, _shown(name, raw), default)
        return default


def _cache_ttl() -> int:
    """AOA_CACHE_TTL: the API offers 5m or 1h only. Tolerant on purpose (a bad value must not stop the worker),
    but never silent: anything other than 300 or 3600 logs the TTL it was turned into."""
    raw = _raw("AOA_CACHE_TTL")
    if raw is None:
        return 300
    try:
        v = int(raw)
    except ValueError:
        log.warning("AOA_CACHE_TTL=%r is not an integer; using 300", raw)
        return 300
    if v in (300, 3600):
        return v
    chosen = 3600 if v > 300 else 300
    log.warning("AOA_CACHE_TTL=%d is not 300 or 3600 (the API offers 5m or 1h); using %d", v, chosen)
    return chosen


def _hour(name: str, default: int) -> int:
    v = _int(name, default)
    if not 0 <= v <= 23:
        log.warning("%s=%d is not an hour (0-23); using %d", name, v, default)
        return default
    return v


def _positive(name: str, default: int) -> int:
    v = _int(name, default)
    if v <= 0:
        log.warning("%s=%d must be positive; using %d", name, v, default)
        return default
    return v


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
    review_settle_seconds: int
    quiet_start_hour: int
    quiet_end_hour: int
    # sandbox
    shell_timeout_seconds: int
    approval_nudge_seconds: int
    review_thinking: str
    approval_expire_seconds: int

    @classmethod
    def from_env(cls) -> "Settings":
        sonnet = os.environ.get("AOA_MODEL", "claude-sonnet-5-5")
        max_tokens = _int("AOA_MAX_TOKENS", 16000)
        # extended thinking for PR reviews: the effort level ("low".."max"); anything else (default) = off. The
        # current models take adaptive thinking steered by effort, not a token budget.
        review_thinking = os.environ.get("AOA_REVIEW_THINKING", "").strip().lower()
        if review_thinking and review_thinking not in ("low", "medium", "high", "xhigh", "max"):
            log.warning(
                "AOA_REVIEW_THINKING=%r is not an effort level (low|medium|high|xhigh|max); thinking stays off", review_thinking)
            review_thinking = ""
        nudge = max(1, _int("AOA_APPROVAL_NUDGE", 4 * 3600))  # expiry must come after the nudge
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
            max_tokens=max_tokens,
            tool_result_max_chars=_int("AOA_TOOL_RESULT_MAX_CHARS", 8000),
            context_hard_limit_tokens=_int("AOA_CONTEXT_HARD_LIMIT", 120_000),
            context_warm_compact_tokens=_int("AOA_CONTEXT_WARM_COMPACT", 50_000),
            cache_ttl_seconds=_cache_ttl(),
            idle_sleep_seconds=_positive("AOA_IDLE_SLEEP", 600),
            batch_window_seconds=_float("AOA_BATCH_WINDOW", 2.0),
            min_update_interval_seconds=_int("AOA_MIN_UPDATE_INTERVAL", 1800),
            review_settle_seconds=max(0, _int("AOA_REVIEW_SETTLE_SECONDS", 300)),
            quiet_start_hour=_hour("AOA_QUIET_START", 22),
            quiet_end_hour=_hour("AOA_QUIET_END", 7),
            shell_timeout_seconds=_positive("AOA_SHELL_TIMEOUT", 60),
            review_thinking=review_thinking,
            approval_nudge_seconds=nudge,
            approval_expire_seconds=max(nudge + 1, _int("AOA_APPROVAL_EXPIRE", 24 * 3600)),
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
