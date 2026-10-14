"""Run the worker: `python -m aoa.worker`. Needs a Temporal server and ANTHROPIC_API_KEY."""
from __future__ import annotations

import asyncio
import logging
import os

from temporalio.client import Client
from temporalio.worker import Worker

from .activities import AgentActivities
from .config import Settings
from .core import AgentCore
from .llm import AnthropicLLM
from .memory import MemoryStore
from .pr_resolve import clean_stale_workdirs
from .scheduling import TemporalScheduler, ensure_github_schedule
from .search_attrs import ensure_registered
from .store import Store
from .workflows import (
    GithubPollWorkflow, HelperWorkflow, MergePrWorkflow, PrReviewWorkflow, RoutineWorkflow, SessionWorkflow,
)


async def build_worker(settings: Settings, client: Client, core: AgentCore | None = None) -> Worker:
    if core is None:
        core = AgentCore(
            settings, Store(settings.db_path), MemoryStore(settings.memory_dir), AnthropicLLM(),
            TemporalScheduler(client, settings),
        )
    acts = AgentActivities(core, client, settings)
    await ensure_registered(client)  # the PR workflows set Repo/PrNumber/Sha/Session; the server must know them
    return Worker(
        client,
        task_queue=settings.task_queue,
        workflows=[SessionWorkflow, HelperWorkflow, RoutineWorkflow, GithubPollWorkflow, PrReviewWorkflow, MergePrWorkflow],
        activities=acts.all(),
        max_concurrent_activities=20,
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if n := clean_stale_workdirs():
        logging.info("removed %d stale conflict-resolution work dir(s)", n)
    settings = Settings.from_env()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    worker = await build_worker(settings, client)
    if os.environ.get("GITHUB_TOKEN") and os.environ.get("AOA_GITHUB_USER"):
        await ensure_github_schedule(client, settings)
    logging.info("worker up on queue %s (data dir %s)", settings.task_queue, settings.data_dir)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
