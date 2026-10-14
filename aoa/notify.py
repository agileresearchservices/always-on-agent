"""The "do I bug the person?" heuristic, and the only path from agent to client.

send_user is the single tool that crosses the client/server boundary: it writes to the outbox and
clients render whatever kind they receive. This module decides *whether* it goes out now.

Rules:
* replying to a live user turn always goes out
* question, approval, urgent and reaction always go out
* "alert" (worth knowing soon, not an emergency: a refused injection, a failing background task) skips
  the spacing between updates but still waits out quiet hours
* everything else ("update") is held for a digest when it comes too soon after the last one,
  or during quiet hours. Held items ride along with the next update that is allowed through.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .config import Settings
from .store import Store

KINDS = {"reply", "update", "alert", "question", "approval", "urgent", "reaction"}
ALWAYS = {"reply", "question", "approval", "urgent", "reaction"}


def in_quiet_hours(now: datetime, start: int, end: int) -> bool:
    h = now.hour
    return (h >= start or h < end) if start > end else (start <= h < end)


def quiet_end_after(t: datetime, start: int, end: int) -> datetime:
    """The first moment at or after `t` that is not in quiet hours (t itself when it is not quiet)."""
    if not in_quiet_hours(t, start, end):
        return t
    nxt = t.replace(hour=end, minute=0, second=0, microsecond=0)
    return nxt if nxt > t else nxt + timedelta(days=1)


class Notifier:
    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.s = settings

    def digest_count(self, session: str) -> int:
        return len(self.store.kv_get(session, "digest", []))

    def next_flush_at(self, session: str, now: datetime) -> float | None:
        """Epoch seconds when the held digest may go out, or None when nothing is held. Never inside quiet
        hours, never before the minimum spacing since the last send."""
        if not self.store.kv_get(session, "digest", []):
            return None
        last = self.store.kv_get(session, "last_notified_ts", 0.0)
        t = max(now.timestamp(), last + self.s.min_update_interval_seconds)
        local = datetime.fromtimestamp(t, ZoneInfo(self.s.tz))
        return quiet_end_after(local, self.s.quiet_start_hour, self.s.quiet_end_hour).timestamp()

    def flush_digest(self, session: str, now: datetime) -> dict[str, Any]:
        """Deliver held updates as one message once they are allowed out. Idempotent: the client key names the
        exact digest, so a retry after a crash cannot send it twice."""
        held = self.store.kv_get(session, "digest", [])
        if not held:
            return {"status": "empty", "flush_at": None}
        due = self.next_flush_at(session, now)
        if due is not None and due > now.timestamp():
            return {"status": "not_due", "flush_at": due}
        last = self.store.kv_get(session, "last_notified_ts", 0.0)
        sig = hashlib.sha1(json.dumps(held).encode()).hexdigest()[:12]
        body = "Held updates:\n" + "\n".join(f"- {h}" for h in held)
        seq = self.store.outbox_push(session, "update", {"text": body}, client_key=f"digest:{last}:{sig}")
        self.store.kv_set(session, "digest", [])
        self.store.kv_set(session, "last_notified_ts", now.timestamp())
        return {"status": "sent" if seq is not None else "duplicate", "flush_at": None}

    def send(self, session: str, kind: str, text: str, now: datetime, client_key: str | None = None) -> dict[str, Any]:
        if kind not in KINDS:
            return {"status": "error", "error": f"unknown kind '{kind}'. Use one of: {sorted(KINDS)}"}
        text = text.strip()
        if not text and kind != "reaction":
            return {"status": "error", "error": "empty message"}

        if kind not in ALWAYS:
            last = self.store.kv_get(session, "last_notified_ts", 0.0)
            too_soon = (now.timestamp() - last) < self.s.min_update_interval_seconds
            quiet = in_quiet_hours(now.astimezone(ZoneInfo(self.s.tz)), self.s.quiet_start_hour, self.s.quiet_end_hour)
            if quiet or (too_soon and kind != "alert"):
                held = self.store.kv_get(session, "digest", [])
                held.append(text)
                self.store.kv_set(session, "digest", held[-20:])
                return {"status": "held", "reason": "quiet hours" if quiet else "too soon after the last update",
                        "note": "Included with your next update that goes out."}

        held = self.store.kv_get(session, "digest", [])
        body = text
        if held and kind in ("update", "reply", "alert"):
            body = text + "\n\nEarlier updates:\n" + "\n".join(f"- {h}" for h in held)
            self.store.kv_set(session, "digest", [])
        seq = self.store.outbox_push(session, kind, {"text": body}, client_key=client_key)
        if seq is None:
            return {"status": "duplicate", "note": "this exact send was already delivered"}
        if kind != "reaction":
            self.store.kv_set(session, "last_notified_ts", now.timestamp())
        return {"status": "sent", "seq": seq}
