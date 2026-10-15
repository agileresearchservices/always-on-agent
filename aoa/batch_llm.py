"""An LLM that sends its calls through the Message Batches API (half price, results within minutes to hours).

Used by `python -m evals --batch`. A run is a multi-step agent loop, but the runs are independent, so each
"round" (every run's next model call) goes out as one batch: the coordinator waits until requests stop arriving
(the runs are all blocked on a response), submits them together, polls until the batch ends, resolves each
caller and the runs carry on. AgentCore and the loop are unchanged; they just see a slow model.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import sys
from typing import Any, Callable

from .llm import AnthropicLLM, LLMResponse, parse_message

log = logging.getLogger(__name__)


class BatchError(Exception):
    pass


class BatchCoordinator:
    def __init__(self, client: Any | None = None, *, settle: float = 2.0, poll: float = 15.0,
                 progress: Callable[[str], None] | None = None):
        if client is None:
            import anthropic

            client = anthropic.AsyncAnthropic()
        self.client, self.settle, self.poll = client, settle, poll
        self.progress = progress or (lambda m: print(m, file=sys.stderr, flush=True))
        self.batches = 0
        self.requests = 0
        self._pending: list[tuple[str, dict[str, Any], asyncio.Future]] = []
        self._ids = itertools.count(1)
        self._driver: asyncio.Task | None = None

    async def submit(self, params: dict[str, Any], model: str) -> LLMResponse:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending.append((f"r{next(self._ids)}", params, fut))
        if self._driver is None or self._driver.done():
            self._driver = asyncio.create_task(self._drive())
        resp = await fut
        return parse_message(resp, model)

    async def _drive(self) -> None:
        try:
            await self._drive_loop()
        except BaseException as e:  # noqa: BLE001 - never die with callers still waiting on a future
            err = e if isinstance(e, Exception) else BatchError("batch driver was cancelled")
            for _, _, fut in self._pending:
                if not fut.done():
                    fut.set_exception(err)
            self._pending = []
            if not isinstance(e, Exception):
                raise

    async def _retry(self, what: str, call: Callable[[], Any], tries: int = 5) -> Any:
        """A blip talking to the API must not throw away a batch that is already running (and billed)."""
        for attempt in range(tries):
            try:
                return await call()
            except Exception as e:  # noqa: BLE001
                if attempt == tries - 1:
                    raise
                wait = min(60.0, self.poll * (2 ** attempt))
                log.warning("batch %s failed (%s); retrying in %.0fs", what, type(e).__name__, wait)
                await asyncio.sleep(wait)

    async def _drive_loop(self) -> None:
        while self._pending:
            seen = -1
            while seen != len(self._pending):  # wait for a quiet moment: every active run is now waiting
                seen = len(self._pending)
                await asyncio.sleep(self.settle)
            batch, self._pending = self._pending, []
            try:
                results = await self._run_batch([(cid, p) for cid, p, _ in batch])
            except Exception as e:  # noqa: BLE001 - the whole batch failed: fail its callers, not the process
                for _, _, fut in batch:
                    if not fut.done():
                        fut.set_exception(e)
                continue
            for cid, _, fut in batch:
                if fut.done():  # the caller gave up (cancelled / timed out): nothing to deliver
                    continue
                r = results.get(cid)
                if r is None:
                    fut.set_exception(BatchError(f"no result for request {cid}"))
                elif isinstance(r, Exception):
                    fut.set_exception(r)
                else:
                    fut.set_result(r)

    async def _run_batch(self, reqs: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
        b = await self._retry("create", lambda: self.client.messages.batches.create(
            requests=[{"custom_id": cid, "params": params} for cid, params in reqs]))
        self.batches += 1
        self.requests += len(reqs)
        self.progress(f"batch {b.id}: {len(reqs)} requests submitted")
        while b.processing_status != "ended":
            await asyncio.sleep(self.poll)
            b = await self._retry("status check", lambda: self.client.messages.batches.retrieve(b.id))
            c = b.request_counts
            self.progress(f"batch {b.id}: {b.processing_status} (done {c.succeeded + c.errored + c.canceled + c.expired}/{len(reqs)})")
        out: dict[str, Any] = {}

        async def fetch() -> dict[str, Any]:
            got: dict[str, Any] = {}
            async for entry in await self.client.messages.batches.results(b.id):
                got[entry.custom_id] = entry.result
            return got

        for cid, res in (await self._retry("results", fetch)).items():
            if res.type == "succeeded":
                out[cid] = res.message
            else:
                detail = getattr(getattr(res, "error", None), "error", None)
                msg = getattr(detail, "message", None) or res.type
                out[cid] = BatchError(f"batch request {res.type}: {msg}")
        return out


class BatchedLLM(AnthropicLLM):
    """Same request building as AnthropicLLM; the call itself goes through the shared coordinator. Unlike the
    synchronous client there is no retry-without-schema: a schema the API rejects fails that run, which is what
    an eval should show."""

    def __init__(self, coordinator: BatchCoordinator, cache_ttl_seconds: int | None = None):
        super().__init__(client=coordinator.client, cache_ttl_seconds=cache_ttl_seconds)
        self.coordinator = coordinator

    async def create(self, *, output_format: dict[str, Any] | None = None, **params: Any) -> LLMResponse:
        kwargs = self.build_kwargs(output_format=output_format, **params)
        return await self.coordinator.submit(kwargs, params["model"])
