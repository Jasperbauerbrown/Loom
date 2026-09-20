"""The scheduler (03 §11.1): schedule rows, a 30s tick, catch-up on startup.

`Scheduler` owns the `schedules` table and nothing else. Firing is delegated to
`triggers.router.route`, so a schedule firing, a connector event and a thread event take
exactly one write path to "inbox row + board item".

Every mutation emits its WS event (`schedule.created|updated|deleted`, data: the row),
so an open Settings → Triggers screen and the backend never disagree.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import zoneinfo
from typing import Any

from .. import db
from ..ids import ulid
from .router import TriggerEvent, route
from .spec import UTC, Spec, SpecError, parse

log = logging.getLogger(__name__)

POLICIES = ("interrupt", "queue")
TITLE_CHARS = 40


class SchedulerError(Exception):
    """A schedule that is refused: unknown target, bad policy, or a cap. (A spec that
    does not parse raises `SpecError`; callers map that to 400 and this to 409.)"""


class Scheduler:
    def __init__(self, app) -> None:  # type: ignore[no-untyped-def]
        self.app = app
        self._task: asyncio.Task | None = None

    # -- settings -----------------------------------------------------------------
    @property
    def tz_name(self) -> str:
        return str(self.app.settings.get("general.timezone") or "UTC")

    @property
    def tz(self) -> _dt.tzinfo:
        try:
            return zoneinfo.ZoneInfo(self.tz_name)
        except Exception:
            return UTC

    def parse(self, spec: str) -> Spec:
        return parse(spec, tz=self.tz_name)

    def local(self, iso: str | None) -> str:
        """An ISO UTC timestamp rendered in the user's timezone, for tool output and the UI."""
        if not iso:
            return "-"
        try:
            moment = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        except ValueError:
            return iso
        return moment.astimezone(self.tz).strftime("%Y-%m-%d %H:%M")

    # -- reads --------------------------------------------------------------------
    def get(self, schedule_id: str) -> dict[str, Any] | None:
        return db.row_to_dict(self.app.db.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone())

    def list(self, include_disabled: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM schedules"
        if not include_disabled:
            sql += " WHERE enabled=1"
        sql += " ORDER BY next_run_at IS NULL, next_run_at, created_at"
        return [db.row_to_dict(r) for r in self.app.db.execute(sql)]  # type: ignore[misc]

    def active_count(self) -> int:
        return int(self.app.db.execute("SELECT COUNT(*) FROM schedules WHERE enabled=1").fetchone()[0])

    # -- validation ---------------------------------------------------------------
    def _check_target(self, target: str) -> str:
        target = (target or "desk").strip() or "desk"
        if target != "desk" and self.app.conversation(target) is None:
            raise SchedulerError(f"target conversation {target!r} does not exist")
        return target

    def _check_policy(self, policy: str | None) -> str | None:
        if policy in (None, ""):
            return None
        if policy not in POLICIES:
            raise SchedulerError(f"policy must be one of {list(POLICIES)}")
        return policy

    def _check_interval(self, spec: Spec) -> None:
        minimum = int(self.app.settings.get("triggers.min_interval_minutes"))
        gap = spec.min_interval()
        if gap is not None and gap < _dt.timedelta(minutes=minimum):
            raise SchedulerError(
                f"{spec.describe()!r} fires every {_human(gap)}, below min_interval_minutes ({minimum})"
            )

    def _first_run(self, spec: Spec) -> str:
        nxt = spec.next_after(_dt.datetime.now(UTC))
        if nxt is None:
            raise SchedulerError(f"{spec.describe()!r} has no next firing (a one-shot time in the past?)")
        return _iso(nxt)

    # -- writes -------------------------------------------------------------------
    def create(
        self,
        spec: str,
        target: str = "desk",
        payload: str = "",
        policy: str | None = None,
        created_by: str = "user",
    ) -> dict[str, Any]:
        parsed = self.parse(spec)  # SpecError → 400 / error result
        payload = (payload or "").strip()
        if not payload:
            raise SchedulerError("payload is required: it is the text the turn receives")
        target = self._check_target(target)
        policy = self._check_policy(policy)
        self._check_interval(parsed)
        cap = int(self.app.settings.get("triggers.max_active_schedules"))
        if self.active_count() >= cap:
            raise SchedulerError(f"max_active_schedules ({cap}) reached; delete or disable one first")
        row_id = ulid()
        now = db.now_iso()
        self.app.db.execute(
            "INSERT INTO schedules(id, spec, target, payload, policy, created_by, enabled, next_run_at, runs, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?, 0, ?)",
            (row_id, parsed.describe(), target, payload, policy, created_by, self._first_run(parsed), now),
        )
        row = self.get(row_id)
        self.app.bus.emit("schedule.created", row)
        return row  # type: ignore[return-value]

    def update(self, schedule_id: str, **fields: Any) -> dict[str, Any]:
        """Change spec / target / payload / policy / enabled. Anything that affects the
        timing re-validates and recomputes `next_run_at`."""
        row = self.get(schedule_id)
        if row is None:
            raise KeyError(schedule_id)
        unknown = set(fields) - {"spec", "target", "payload", "policy", "enabled"}
        if unknown:
            raise SchedulerError(f"cannot update {sorted(unknown)}")
        updates: dict[str, Any] = {}
        parsed = self.parse(row["spec"])
        if "spec" in fields:
            parsed = self.parse(str(fields["spec"]))
            self._check_interval(parsed)
            updates["spec"] = parsed.describe()
        if "target" in fields:
            updates["target"] = self._check_target(str(fields["target"]))
        if "payload" in fields:
            payload = str(fields["payload"] or "").strip()
            if not payload:
                raise SchedulerError("payload cannot be empty")
            updates["payload"] = payload
        if "policy" in fields:
            updates["policy"] = self._check_policy(fields["policy"])
        enabling = False
        if "enabled" in fields:
            enabled = 1 if fields["enabled"] else 0
            enabling = bool(enabled) and not row["enabled"]
            if enabling and self.active_count() >= int(self.app.settings.get("triggers.max_active_schedules")):
                cap = int(self.app.settings.get("triggers.max_active_schedules"))
                raise SchedulerError(f"max_active_schedules ({cap}) reached; delete or disable one first")
            updates["enabled"] = enabled
        if "spec" in fields or enabling or (row["next_run_at"] is None and updates.get("enabled", row["enabled"])):
            updates["next_run_at"] = self._first_run(parsed)
        if updates:
            sql = ", ".join(f"{k}=?" for k in updates)
            self.app.db.execute(f"UPDATE schedules SET {sql} WHERE id=?", (*updates.values(), schedule_id))
        row = self.get(schedule_id)
        self.app.bus.emit("schedule.updated", row)
        return row  # type: ignore[return-value]

    def enable(self, schedule_id: str, enabled: bool = True) -> dict[str, Any]:
        return self.update(schedule_id, enabled=enabled)

    def delete(self, schedule_id: str) -> dict[str, Any]:
        row = self.get(schedule_id)
        if row is None:
            raise KeyError(schedule_id)
        self.app.db.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))
        self.app.bus.emit("schedule.deleted", row)
        return row

    # -- firing -------------------------------------------------------------------
    def fire(self, row: dict[str, Any], *, catch_up: bool = False, advance: bool = True) -> dict[str, Any]:
        """Persist first (item + inbox row, via the event router), then move the row on.

        `advance=False` is `run_now`: the firing happens but the schedule does not shift.
        """
        schedule_id = row["id"]
        target = row["target"]
        if target != "desk" and self.app.conversation(target) is None:
            log.warning("schedule %s targets missing conversation %s; disabling", schedule_id, target)
            self.app.db.execute("UPDATE schedules SET enabled=0, next_run_at=NULL WHERE id=?", (schedule_id,))
            self.app.bus.emit("schedule.updated", self.get(schedule_id))
            return {"fired": False, "reason": "target missing"}

        payload = row["payload"] or ""
        label = _label(payload) or row["spec"]
        title = f"Schedule: {label}" + (" (missed while offline)" if catch_up else "")
        routed = route(
            self.app,
            TriggerEvent(
                kind="schedule",
                source=f"schedule:{schedule_id}",
                target=target,
                title=title,
                content=payload,
                meta={"title": label, "schedule_id": schedule_id, "catch_up": catch_up},
                policy=row["policy"] or None,
            ),
        )

        now = _dt.datetime.now(UTC)
        updates: dict[str, Any] = {"last_run_at": _iso(now), "runs": int(row["runs"] or 0) + 1}
        if advance:
            parsed = self.parse(row["spec"])
            if parsed.is_one_shot:
                updates["enabled"] = 0
                updates["next_run_at"] = None
            else:
                nxt = parsed.next_after(now)
                updates["next_run_at"] = _iso(nxt) if nxt else None
                if nxt is None:
                    updates["enabled"] = 0
        sql = ", ".join(f"{k}=?" for k in updates)
        self.app.db.execute(f"UPDATE schedules SET {sql} WHERE id=?", (*updates.values(), schedule_id))
        self.app.bus.emit("schedule.updated", self.get(schedule_id))
        return {"fired": True, **routed}

    def run_now(self, schedule_id: str) -> dict[str, Any]:
        row = self.get(schedule_id)
        if row is None:
            raise KeyError(schedule_id)
        self.fire(row, advance=False)
        return self.get(schedule_id)  # type: ignore[return-value]

    async def tick(self) -> int:
        """Fire every enabled row that is due. One bad row never stops the others."""
        now_iso = db.now_iso()
        due = [
            db.row_to_dict(r)
            for r in self.app.db.execute(
                "SELECT * FROM schedules WHERE enabled=1 AND next_run_at IS NOT NULL AND next_run_at<=? "
                "ORDER BY next_run_at",
                (now_iso,),
            )
        ]
        fired = 0
        for row in due:
            try:
                if self.fire(row).get("fired"):  # type: ignore[union-attr]
                    fired += 1
            except Exception:
                log.exception("schedule %s failed to fire", row["id"])  # type: ignore[index]
        return fired

    # -- lifecycle ----------------------------------------------------------------
    async def start(self) -> None:
        self.catch_up()
        self._task = self.app.spawn("scheduler", self._loop())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: B014 - a cancelled loop is not an error
                pass

    def catch_up(self) -> int:
        """Rows whose `next_run_at` passed while the backend was down: fire once if the
        miss is inside `triggers.schedule_catchup_hours`, otherwise just reschedule."""
        hours = int(self.app.settings.get("triggers.schedule_catchup_hours"))
        now = _dt.datetime.now(UTC)
        window = now - _dt.timedelta(hours=hours)
        fired = 0
        for row in self.list(include_disabled=False):
            due = _parse_iso(row.get("next_run_at"))
            if due is None or due > now:
                continue
            try:
                if hours > 0 and due >= window:
                    if self.fire(row, catch_up=True).get("fired"):
                        fired += 1
                else:
                    parsed = self.parse(row["spec"])
                    nxt = parsed.next_after(now)
                    self.app.db.execute(
                        "UPDATE schedules SET next_run_at=?, enabled=? WHERE id=?",
                        (_iso(nxt) if nxt else None, 0 if nxt is None else 1, row["id"]),
                    )
                    self.app.bus.emit("schedule.updated", self.get(row["id"]))
            except Exception:
                log.exception("catch-up failed for schedule %s", row["id"])
        if fired:
            log.info("scheduler fired %d missed schedule(s)", fired)
        return fired

    async def _loop(self) -> None:
        while not self.app.shutting_down:
            try:
                await asyncio.sleep(max(1.0, float(self.app.settings.get("triggers.scheduler_tick_seconds"))))
            except asyncio.CancelledError:
                return
            try:
                await self.tick()
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("scheduler tick failed")


# -- small helpers ---------------------------------------------------------------------

def _iso(moment: _dt.datetime) -> str:
    return moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(text: str | None) -> _dt.datetime | None:
    if not text:
        return None
    try:
        moment = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _label(payload: str) -> str:
    line = (payload or "").strip().splitlines()[0] if (payload or "").strip() else ""
    return line[:TITLE_CHARS].strip()


def _human(gap: _dt.timedelta) -> str:
    seconds = int(gap.total_seconds())
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


__all__ = ["Scheduler", "SchedulerError", "SpecError"]
