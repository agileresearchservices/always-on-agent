"""Scheduled flag scan (#64): mine the stored traces for bad outcomes on a timer and tell the person once.

Free (SQLite only, no model call) and it never starts a proposal: proposing costs tokens and a person decides when.
"""
from __future__ import annotations

import hashlib
from collections import Counter
from typing import Any

from . import flag
from .config import Settings
from .store import Store

MAX_LISTED = 8


def _kind(reason: str) -> str:
    return reason.split(":", 1)[0]


def digest_text(added: list[dict[str, Any]]) -> str:
    kinds = Counter(_kind(f["reason"]) for f in added)
    lines = [f":mag: *{len(added)} new flagged outcome(s)* found in the stored traces: "
             + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))]
    for f in added[:MAX_LISTED]:
        lines.append(f"• `{f['conv_id']}` {f['reason']}: {f['evidence'][:140]}")
    if len(added) > MAX_LISTED:
        lines.append(f"…and {len(added) - MAX_LISTED} more (`python -m aoa flags`).")
    lines.append("Nothing was proposed. To draft an eval case from one: `python -m aoa propose --limit 1` "
                 "(a few cents of model calls and eval runs); a card arrives here to Accept or Reject.")
    return "\n".join(lines)


def run_flag_scan(store: Store, settings: Settings, session: str) -> dict[str, Any]:
    added = flag.scan(store, settings)
    if added:  # the key comes from the rows, so a retried activity cannot post the same digest twice
        ident = hashlib.sha256("|".join(sorted(f"{f['conv_id']}/{f['reason']}" for f in added)).encode()).hexdigest()[:16]
        store.outbox_push(session, "alert", {"text": digest_text(added)}, client_key=f"flagscan:{ident}")
    return {"new": len(added)}
