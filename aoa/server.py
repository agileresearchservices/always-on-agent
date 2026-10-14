"""HTTP edge: triggers in, outbox out. Run with `uvicorn aoa.server:app`.

Auth is deliberately minimal (optional shared secrets via env). Put it behind real auth before
exposing it beyond localhost.
"""
from __future__ import annotations

import asyncio
import hmac
import os
import re
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from .config import Settings
from .delivery import deliver_event, signal_typing
from .events import Event, approval_event, user_event
from .github_monitor import GithubError, check_repo_in_scope
from .pr_merge import OFFER_ID_RE, OFFER_KEY
from .store import Store

settings = Settings.from_env()
store = Store(settings.db_path)


def check_auth_configured() -> None:
    """Refuse to start without both shared secrets unless insecure mode is explicitly requested."""
    if os.environ.get("AOA_ALLOW_INSECURE") == "1":
        return
    missing = [v for v in ("AOA_API_TOKEN", "AOA_WEBHOOK_SECRET") if not os.environ.get(v)]
    if missing:
        raise RuntimeError(
            f"{' and '.join(missing)} not set, so the API would accept unauthenticated requests. "
            "Set them, or set AOA_ALLOW_INSECURE=1 for local development only."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_auth_configured()
    app.state.client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    yield


app = FastAPI(title="aoa", lifespan=lifespan)


def _same(a: str | None, b: str) -> bool:
    return a is not None and hmac.compare_digest(a.encode(), b.encode())


def require_token(authorization: str | None = Header(default=None)) -> None:
    token = os.environ.get("AOA_API_TOKEN")
    if token and not _same((authorization or "").removeprefix("Bearer "), token):
        raise HTTPException(401, "missing or bad bearer token")


def require_webhook_secret(x_aoa_secret: str | None = Header(default=None)) -> None:
    secret = os.environ.get("AOA_WEBHOOK_SECRET")
    if secret and not _same(x_aoa_secret, secret):
        raise HTTPException(401, "missing or bad X-AOA-Secret")


class Message(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    message_id: str | None = None  # client-generated; makes retries safe


class Webhook(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    session: str = "main"
    kind: str = "message"
    dedupe_key: str | None = None  # e.g. the provider's event id


class Decision(BaseModel):
    approve: bool


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/sessions/{sid}/messages", status_code=202, dependencies=[Depends(require_token)])
async def post_message(sid: str, m: Message) -> dict[str, Any]:
    ev = user_event(m.text, f"user:{m.message_id}" if m.message_id else None)
    await deliver_event(app.state.client, settings, sid, ev.to_dict())
    return {"accepted": True, "dedupe_key": ev.dedupe_key}


@app.post("/webhooks/{source}", status_code=202, dependencies=[Depends(require_webhook_secret)])
async def post_webhook(source: str, w: Webhook) -> dict[str, Any]:
    ev = Event(
        source=source, kind=w.kind, text=w.text, priority=1, trusted=False,  # external => untrusted
        dedupe_key=f"{source}:{w.dedupe_key}" if w.dedupe_key else f"{source}:{uuid.uuid4()}",
    )
    await deliver_event(app.state.client, settings, w.session, ev.to_dict())
    return {"accepted": True}


@app.post("/sessions/{sid}/typing", status_code=202, dependencies=[Depends(require_token)])
async def typing(sid: str) -> dict[str, bool]:
    await signal_typing(app.state.client, settings, sid)  # lets the server warm the prompt cache early
    return {"accepted": True}


@app.get("/sessions/{sid}/outbox", dependencies=[Depends(require_token)])
async def outbox(sid: str, after: int = 0, wait: float = 20.0) -> dict[str, Any]:
    """Long poll: returns as soon as something newer than `after` exists, or after `wait` seconds."""
    deadline = asyncio.get_event_loop().time() + min(max(wait, 0), 60)
    while True:
        items = store.outbox_since(sid, after)
        if items or asyncio.get_event_loop().time() >= deadline:
            return {"items": items}
        await asyncio.sleep(0.4)


@app.post("/sessions/{sid}/approvals/{approval_id}", dependencies=[Depends(require_token)])
async def decide(sid: str, approval_id: str, d: Decision) -> dict[str, Any]:
    rec = store.approval_get(approval_id)
    if rec is None or rec["session"] != sid:
        raise HTTPException(404, "no such approval")
    if not store.approval_resolve(approval_id, d.approve):
        status = (store.approval_get(approval_id) or rec)["status"]  # re-read: it may have lapsed just now
        if status == "expired":
            raise HTTPException(409, "this approval expired unanswered; ask the agent again")
        raise HTTPException(409, f"already {status}")
    act = rec["action"]
    summary = f"{act.get('tool')} to {act['channel']}" if act.get("channel") else f"{act.get('tool')} '{act.get('name')}'"
    await deliver_event(app.state.client, settings, sid, approval_event(approval_id, d.approve, summary).to_dict())
    return {"status": "approved" if d.approve else "denied"}


class MergeRequest(BaseModel):
    card_ts: str | None = Field(default=None, max_length=40)  # Slack ts of the card, so the result can update it


@app.post("/sessions/{sid}/merges/{offer_id}", status_code=202, dependencies=[Depends(require_token)])
async def merge(sid: str, offer_id: str, m: MergeRequest) -> dict[str, Any]:
    """The person clicked [Merge]. Only a stored offer (an approved review of one PR at one commit) can be
    merged; the click names it and nothing else. Idempotent while a merge runs: a second click gets 409."""
    offer = store.kv_get(sid, OFFER_KEY + offer_id) if re.fullmatch(OFFER_ID_RE, offer_id) else None
    if not offer or offer.get("verdict") != "approve":
        raise HTTPException(404, "no such merge offer")
    try:
        check_repo_in_scope(offer["repo"])
    except GithubError as e:
        raise HTTPException(403, str(e)) from e
    try:
        await app.state.client.start_workflow(
            "MergePrWorkflow",
            {"session": sid, "offer_id": offer_id, "offer": offer, "card_ts": m.card_ts},
            id=f"merge-{sid}-{offer_id}", task_queue=settings.task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,  # running: refuse the double click
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,  # finished: a retry may run (it re-checks GitHub)
        )
    except WorkflowAlreadyStartedError as e:
        raise HTTPException(409, "a merge of this PR is already running") from e
    return {"status": "started"}


@app.get("/sessions/{sid}/state", dependencies=[Depends(require_token)])
async def state(sid: str) -> dict[str, Any]:
    from .delivery import session_workflow_id

    live: dict[str, Any] = {}
    try:
        live = await app.state.client.get_workflow_handle(session_workflow_id(sid)).query("status")
    except Exception:  # noqa: BLE001 - workflow may not be running yet
        live = {"running": False}
    return {"workflow": live, "usage": store.usage_totals(sid), "routines": store.routine_list(sid)}
