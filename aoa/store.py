"""SQLite state shared by every worker process.

Conversations, idempotency records for durable steps, event dedupe, the client outbox,
usage/cache accounting, approvals, routines and small key/value state all live here.
Workflow history in Temporal only carries *orchestration*; the heavy data stays here.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  conv_id TEXT NOT NULL, idx INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
  PRIMARY KEY (conv_id, idx));
CREATE TABLE IF NOT EXISTS steps (
  conv_id TEXT NOT NULL, step_key TEXT NOT NULL, result TEXT NOT NULL,
  PRIMARY KEY (conv_id, step_key));
CREATE TABLE IF NOT EXISTS events (dedupe_key TEXT PRIMARY KEY, session TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS outbox (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL, kind TEXT NOT NULL,
  payload TEXT NOT NULL, client_key TEXT UNIQUE, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conv_id TEXT NOT NULL, ts REAL NOT NULL, model TEXT NOT NULL,
  input_tokens INTEGER, output_tokens INTEGER, cache_read INTEGER, cache_write INTEGER);
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY, session TEXT NOT NULL, action TEXT NOT NULL, status TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS routines (
  session TEXT NOT NULL, name TEXT NOT NULL, cron TEXT NOT NULL, prompt TEXT NOT NULL,
  PRIMARY KEY (session, name));
CREATE TABLE IF NOT EXISTS kv (session TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
  PRIMARY KEY (session, key));
"""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK")
                raise
            else:
                c.execute("COMMIT")

    # ---- messages -------------------------------------------------------
    @staticmethod
    def _next_idx(c: sqlite3.Connection, conv_id: str) -> int:
        row = c.execute("SELECT MAX(idx) AS m FROM messages WHERE conv_id=?", (conv_id,)).fetchone()
        return (row["m"] if row["m"] is not None else -1) + 1

    def append_message(self, conv_id: str, role: str, content: list[dict[str, Any]]) -> int:
        with self._tx() as c:
            idx = self._next_idx(c, conv_id)
            c.execute("INSERT INTO messages VALUES (?,?,?,?)", (conv_id, idx, role, json.dumps(content)))
            return idx

    def _append_user_blocks(self, c: sqlite3.Connection, conv_id: str, blocks: list[dict[str, Any]]) -> None:
        last = c.execute(
            "SELECT idx, role, content FROM messages WHERE conv_id=? ORDER BY idx DESC LIMIT 1",
            (conv_id,),
        ).fetchone()
        if last and last["role"] == "user":
            merged = json.loads(last["content"]) + blocks
            c.execute(
                "UPDATE messages SET content=? WHERE conv_id=? AND idx=?",
                (json.dumps(merged), conv_id, last["idx"]),
            )
        else:
            c.execute(
                "INSERT INTO messages VALUES (?,?,?,?)",
                (conv_id, self._next_idx(c, conv_id), "user", json.dumps(blocks)),
            )

    def append_user_content(self, conv_id: str, blocks: list[dict[str, Any]]) -> None:
        """Append blocks to the conversation as user content, merging into a trailing user message.

        This is how steering works: a new user event arriving mid-turn rides along with the
        tool results of the step in flight instead of waiting in a queue.
        """
        with self._tx() as c:
            self._append_user_blocks(c, conv_id, blocks)

    def commit_step(
        self,
        conv_id: str,
        step_key: str,
        result: Any,
        *,
        role: str | None = None,
        content: list[dict[str, Any]] | None = None,
    ) -> tuple[Any, bool]:
        """Atomically record a durable step: the message it produced AND its idempotency record.

        If the step was already committed (an activity retry after a crash between commit and ack),
        nothing is written and the stored result is returned with fresh=False. This is what makes
        replaying activities safe: no duplicate assistant messages, no double billing.
        """
        with self._tx() as c:
            row = c.execute(
                "SELECT result FROM steps WHERE conv_id=? AND step_key=?", (conv_id, step_key)
            ).fetchone()
            if row:
                return json.loads(row["result"]), False
            if role == "user" and content is not None:
                self._append_user_blocks(c, conv_id, content)
            elif role is not None and content is not None:
                c.execute(
                    "INSERT INTO messages VALUES (?,?,?,?)",
                    (conv_id, self._next_idx(c, conv_id), role, json.dumps(content)),
                )
            c.execute("INSERT INTO steps VALUES (?,?,?)", (conv_id, step_key, json.dumps(result)))
            return result, True

    def load_messages(self, conv_id: str) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT role, content FROM messages WHERE conv_id=? ORDER BY idx", (conv_id,)
            ).fetchall()
        return [{"role": r["role"], "content": json.loads(r["content"])} for r in rows]

    def replace_prefix(self, conv_id: str, keep_from_idx: int, summary_blocks: list[dict[str, Any]]) -> None:
        """Replace messages with idx < keep_from_idx by a single summary message (compaction)."""
        with self._tx() as c:
            c.execute("DELETE FROM messages WHERE conv_id=? AND idx<?", (conv_id, keep_from_idx))
            c.execute(
                "INSERT INTO messages VALUES (?,?,?,?)",
                (conv_id, keep_from_idx - 1 if keep_from_idx > 0 else 0, "user", json.dumps(summary_blocks)),
            )

    def messages_with_idx(self, conv_id: str) -> list[tuple[int, dict[str, Any]]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT idx, role, content FROM messages WHERE conv_id=? ORDER BY idx", (conv_id,)
            ).fetchall()
        return [(r["idx"], {"role": r["role"], "content": json.loads(r["content"])}) for r in rows]

    # ---- idempotent step records -----------------------------------------
    def step_get(self, conv_id: str, step_key: str) -> Any | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT result FROM steps WHERE conv_id=? AND step_key=?", (conv_id, step_key)
            ).fetchone()
        return json.loads(row["result"]) if row else None

    def step_put(self, conv_id: str, step_key: str, result: Any) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO steps VALUES (?,?,?)", (conv_id, step_key, json.dumps(result))
            )

    # ---- events ----------------------------------------------------------
    def add_event_once(self, session: str, dedupe_key: str) -> bool:
        with self._conn() as c:
            try:
                c.execute("INSERT INTO events VALUES (?,?,?)", (dedupe_key, session, time.time()))
                return True
            except sqlite3.IntegrityError:
                return False

    # ---- outbox (what thin clients subscribe to) ----------------------------
    def outbox_push(
        self, session: str, kind: str, payload: dict[str, Any], client_key: str | None = None
    ) -> int | None:
        with self._conn() as c:
            try:
                cur = c.execute(
                    "INSERT INTO outbox (session, kind, payload, client_key, ts) VALUES (?,?,?,?,?)",
                    (session, kind, json.dumps(payload), client_key, time.time()),
                )
                return cur.lastrowid
            except sqlite3.IntegrityError:
                return None

    def outbox_since(self, session: str, after: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT seq, kind, payload, ts FROM outbox WHERE session=? AND seq>? ORDER BY seq LIMIT ?",
                (session, after, limit),
            ).fetchall()
        return [
            {"seq": r["seq"], "kind": r["kind"], "payload": json.loads(r["payload"]), "ts": r["ts"]}
            for r in rows
        ]

    # ---- usage / cache accounting ------------------------------------------
    def usage_add(self, conv_id: str, model: str, usage: dict[str, int]) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO usage (conv_id, ts, model, input_tokens, output_tokens, cache_read, cache_write)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    conv_id, time.time(), model,
                    usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                    usage.get("cache_read_input_tokens", 0), usage.get("cache_creation_input_tokens", 0),
                ),
            )

    def last_usage(self, conv_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM usage WHERE conv_id=? ORDER BY id DESC LIMIT 1", (conv_id,)
            ).fetchone()
        return dict(row) if row else None

    def usage_totals(self, conv_id: str | None = None) -> dict[str, int]:
        q = "SELECT COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o," \
            " COALESCE(SUM(cache_read),0) r, COALESCE(SUM(cache_write),0) w FROM usage"
        args: tuple = ()
        if conv_id:
            q += " WHERE conv_id=?"
            args = (conv_id,)
        with self._conn() as c:
            row = c.execute(q, args).fetchone()
        return {"input": row["i"], "output": row["o"], "cache_read": row["r"], "cache_write": row["w"]}

    # ---- approvals -------------------------------------------------------
    def approval_request(self, approval_id: str, session: str, action: dict[str, Any]) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO approvals VALUES (?,?,?,?,?)",
                (approval_id, session, json.dumps(action), "pending", time.time()),
            )

    def approval_get(self, approval_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["action"] = json.loads(d["action"])
        return d

    def approval_resolve(self, approval_id: str, approved: bool) -> bool:
        """Resolve once. Returns False if it was already resolved or does not exist."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE approvals SET status=? WHERE id=? AND status='pending'",
                ("approved" if approved else "denied", approval_id),
            )
            return cur.rowcount == 1

    # ---- routines ----------------------------------------------------------
    def routine_put(self, session: str, name: str, cron: str, prompt: str) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO routines VALUES (?,?,?,?)", (session, name, cron, prompt))

    def routine_delete(self, session: str, name: str) -> bool:
        with self._conn() as c:
            return c.execute("DELETE FROM routines WHERE session=? AND name=?", (session, name)).rowcount > 0

    def routine_list(self, session: str) -> list[dict[str, str]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT name, cron, prompt FROM routines WHERE session=? ORDER BY name", (session,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- kv ----------------------------------------------------------------
    def kv_get(self, session: str, key: str, default: Any = None) -> Any:
        with self._conn() as c:
            row = c.execute("SELECT value FROM kv WHERE session=? AND key=?", (session, key)).fetchone()
        return json.loads(row["value"]) if row else default

    def kv_set(self, session: str, key: str, value: Any) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?)", (session, key, json.dumps(value)))
