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


EVAL_SCHEDULE_ID = "aoa-eval-run"


async def ensure_eval_schedule(client: Client, settings: Settings) -> None:
    """Create, update or delete the scheduled behaviour-eval run (#61). Real-model runs, so it exists only while
    AOA_EVAL_SCHEDULE_HOURS > 0 and an API key is configured; setting it back to 0 removes the schedule."""
    import os
    from datetime import timedelta

    from temporalio.client import ScheduleIntervalSpec

    handle = client.get_schedule_handle(EVAL_SCHEDULE_ID)
    if settings.eval_schedule_hours <= 0 or not os.environ.get("ANTHROPIC_API_KEY"):
        try:
            await handle.delete()
        except RPCError as e:
            if e.status != RPCStatusCode.NOT_FOUND:
                raise
        return
    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            "EvalRunWorkflow", {"session": os.environ.get("AOA_SESSION", "main")},
            id=f"{EVAL_SCHEDULE_ID}-run", task_queue=settings.task_queue, execution_timeout=timedelta(minutes=40),
        ),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(hours=settings.eval_schedule_hours))]),
    )
    try:
        await client.create_schedule(EVAL_SCHEDULE_ID, schedule)
    except ScheduleAlreadyRunningError:
        await handle.update(lambda _: ScheduleUpdate(schedule=schedule))


FLAG_SCHEDULE_ID = "aoa-flag-scan"


async def ensure_flag_schedule(client: Client, settings: Settings) -> None:
    """Create, update or delete the scheduled flag scan (#64). It reads SQLite and posts a digest; no tokens are spent,
    so it needs no key. AOA_FLAG_SCAN_HOURS=0 removes the schedule."""
    import os
    from datetime import timedelta

    from temporalio.client import ScheduleIntervalSpec

    handle = client.get_schedule_handle(FLAG_SCHEDULE_ID)
    if settings.flag_scan_hours <= 0:
        try:
            await handle.delete()
        except RPCError as e:
            if e.status != RPCStatusCode.NOT_FOUND:
                raise
        return
    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            "FlagScanWorkflow", {"session": os.environ.get("AOA_SESSION", "main")},
            id=f"{FLAG_SCHEDULE_ID}-run", task_queue=settings.task_queue, execution_timeout=timedelta(minutes=15),
        ),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(hours=settings.flag_scan_hours))]),
    )
    try:
        await client.create_schedule(FLAG_SCHEDULE_ID, schedule)
    except ScheduleAlreadyRunningError:
        await handle.update(lambda _: ScheduleUpdate(schedule=schedule))
