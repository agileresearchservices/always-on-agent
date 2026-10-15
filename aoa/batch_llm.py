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
                r = results.get(cid)
                if r is None:
                    fut.set_exception(BatchError(f"no result for request {cid}"))
                elif isinstance(r, Exception):
                    fut.set_exception(r)
                else:
                    fut.set_result(r)

    async def _run_batch(self, reqs: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
        b = await self.client.messages.batches.create(
            requests=[{"custom_id": cid, "params": params} for cid, params in reqs])
        self.batches += 1
        self.requests += len(reqs)
        self.progress(f"batch {b.id}: {len(reqs)} requests submitted")
        while b.processing_status != "ended":
            await asyncio.sleep(self.poll)
            b = await self.client.messages.batches.retrieve(b.id)
            c = b.request_counts
            self.progress(f"batch {b.id}: {b.processing_status} (done {c.succeeded + c.errored + c.canceled + c.expired}/{len(reqs)})")
        out: dict[str, Any] = {}
        async for entry in await self.client.messages.batches.results(b.id):
            res = entry.result
            if res.type == "succeeded":
                out[entry.custom_id] = res.message
            else:
                detail = getattr(getattr(res, "error", None), "error", None)
                msg = getattr(detail, "message", None) or res.type
                out[entry.custom_id] = BatchError(f"batch request {res.type}: {msg}")
        return out


class BatchedLLM(AnthropicLLM):
    """Same request building as AnthropicLLM; the call itself goes through the shared coordinator."""

    def __init__(self, coordinator: BatchCoordinator, cache_ttl_seconds: int | None = None):
        super().__init__(client=coordinator.client, cache_ttl_seconds=cache_ttl_seconds)
        self.coordinator = coordinator

    async def create(self, *, output_format: dict[str, Any] | None = None, **params: Any) -> LLMResponse:
        kwargs = self.build_kwargs(output_format=output_format, **params)
        return await self.coordinator.submit(kwargs, params["model"])
