"""Routines as Temporal Schedules: a cron fires RoutineWorkflow, which hands the prompt to the session."""
from __future__ import annotations

import logging

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleSpec,
    ScheduleUpdate,
)
from temporalio.service import RPCError, RPCStatusCode

from .config import Settings

log = logging.getLogger(__name__)

POLL_FLOOR_SECONDS = 30
POLL_DEFAULT_SECONDS = 600


def poll_seconds(raw: str | None) -> int:
    """AOA_GITHUB_POLL_SECONDS -> the poll interval: unset/empty 600, garbage 600 with a warning, floor 30."""
    if raw is None or not raw.strip():
        return POLL_DEFAULT_SECONDS
    try:
        v = int(raw.strip())
    except ValueError:
        log.warning("AOA_GITHUB_POLL_SECONDS=%r is not an integer; using %d", raw, POLL_DEFAULT_SECONDS)
        return POLL_DEFAULT_SECONDS
    if v < POLL_FLOOR_SECONDS:
        log.warning("AOA_GITHUB_POLL_SECONDS=%d is below the %ds floor; using %d", v, POLL_FLOOR_SECONDS, POLL_FLOOR_SECONDS)
        return POLL_FLOOR_SECONDS
    return v


class TemporalScheduler:
    def __init__(self, client: Client, settings: Settings):
        self.client, self.s = client, settings

    @staticmethod
    def schedule_id(session: str, name: str) -> str:
        return f"aoa-routine-{session}-{name}"

    async def create(self, session: str, name: str, cron: str, prompt: str) -> None:
        sid = self.schedule_id(session, name)
        schedule = Schedule(
            action=ScheduleActionStartWorkflow(
                "RoutineWorkflow",
                {"session": session, "name": name, "prompt": prompt},
                id=f"{sid}-fire",
                task_queue=self.s.task_queue,
            ),
            spec=ScheduleSpec(cron_expressions=[cron]),
        )
        try:
            await self.client.create_schedule(sid, schedule)
        except ScheduleAlreadyRunningError:
            await self.client.get_schedule_handle(sid).update(lambda _: ScheduleUpdate(schedule=schedule))

    async def delete(self, session: str, name: str) -> bool:
        try:
            await self.client.get_schedule_handle(self.schedule_id(session, name)).delete()
            return True
        except RPCError as e:
            if e.status == RPCStatusCode.NOT_FOUND:
                return False
            raise


GITHUB_SCHEDULE_ID = "aoa-monitor-github"


async def ensure_github_schedule(client: Client, settings: Settings) -> None:
    """Create (or update) the GitHub PR poll schedule. Interval and user come from env at worker start."""
    import os
    from datetime import timedelta

    from temporalio.client import ScheduleIntervalSpec

    from .delivery import workflow_cfg

    session = os.environ.get("AOA_SESSION", "main")
    every = poll_seconds(os.environ.get("AOA_GITHUB_POLL_SECONDS"))
    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            "GithubPollWorkflow",
            {"session": session, "user": os.environ["AOA_GITHUB_USER"], "cfg": workflow_cfg(settings)},
            id=f"{GITHUB_SCHEDULE_ID}-run", task_queue=settings.task_queue,
            # overlap policy is skip, so a stuck poll would block every later one; cap its life
            execution_timeout=timedelta(minutes=5),
        ),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(seconds=every))]),
    )
    try:
        await client.create_schedule(GITHUB_SCHEDULE_ID, schedule)
    except ScheduleAlreadyRunningError:
        await client.get_schedule_handle(GITHUB_SCHEDULE_ID).update(lambda _: ScheduleUpdate(schedule=schedule))
