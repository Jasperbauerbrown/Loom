"""Triggers: schedules, the event router, and the schedule tools (03 §11).

A trigger is not an automation path. Everything here ends in the same two writes — a
now-board item and an inbox row — and `conversation.post` wakes the one turn primitive.

    app.scheduler                       loom.triggers.scheduler.Scheduler
    route(app, TriggerEvent(...))       the one "event → inbox + item" write path
    route_connector_event(app, ...)     what the connector loader calls

`install` attaches the scheduler and registers `schedule_create` / `schedule_list` /
`schedule_delete`; `start`/`stop` run the tick loop (with catch-up on start).
"""

from __future__ import annotations

from .router import TriggerEvent, route, route_connector_event  # noqa: F401  (re-exported)
from .scheduler import Scheduler, SchedulerError  # noqa: F401
from .spec import Spec, SpecError, parse  # noqa: F401


def install(app) -> None:  # type: ignore[no-untyped-def]
    from ..tools.native import schedule as schedule_tools

    app.scheduler = Scheduler(app)
    for tool in schedule_tools.tools():
        app.tools.register(tool, replace=True)


async def start(app) -> None:  # type: ignore[no-untyped-def]
    if app.scheduler is not None:
        await app.scheduler.start()


async def stop(app) -> None:  # type: ignore[no-untyped-def]
    if app.scheduler is not None:
        await app.scheduler.stop()
