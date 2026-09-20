"""Schedule spec grammar (03 §11.1). ~One parser, no dependency.

    parse("every 15m")            Spec(kind="every")
    parse("daily at 09:00")       Spec(kind="daily")
    parse("weekly on mon,thu at 18:30")
    parse("monthly on 15 at 08:00")
    parse("at 2026-09-26T18:00")  one-shot
    parse("0 9 * * 1-5")          5-field cron: m h dom mon dow

Everything is case-insensitive and whitespace-tolerant. All "next" computations run in
the wall clock of `tz` (an IANA name, `general.timezone`) and return **aware UTC**
datetimes, so a `daily at 09:00` stays at 09:00 local across a DST change.

Nothing here reads settings or the database: `parse(spec, tz=...)` is given the zone by
its caller (the scheduler passes `general.timezone`).
"""

from __future__ import annotations

import datetime as _dt
import re
import zoneinfo
from dataclasses import dataclass

UTC = _dt.timezone.utc

ACCEPTED = (
    "accepted forms: 'every 15m' / 'every 90 minutes' (units s, m, h, d); "
    "'daily at HH:MM'; 'weekly on mon,thu at HH:MM'; 'monthly on 15 at HH:MM'; "
    "'at 2026-09-26T18:00' (one-shot); or a 5-field cron 'm h dom mon dow'"
)


class SpecError(ValueError):
    """A spec that cannot be parsed. `str()` always names the accepted forms."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(f"{message}; {ACCEPTED}")


# -- vocabulary ----------------------------------------------------------------------

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_UNIT_WORDS = {
    "s": "s", "sec": "s", "secs": "s", "second": "s", "seconds": "s",
    "m": "m", "min": "m", "mins": "m", "minute": "m", "minutes": "m",
    "h": "h", "hr": "h", "hrs": "h", "hour": "h", "hours": "h",
    "d": "d", "day": "d", "days": "d",
}
_DAY_NAMES = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2, "weds": 2,
    "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "fri": 4,
    "friday": 4, "sat": 5, "saturday": 5, "sun": 6, "sunday": 6,
}
_DAY_SHORT = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# cron day-of-week is Sunday-first (0 and 7 both mean Sunday)
_CRON_DOW = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
_CRON_MON = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _zone(name: str) -> _dt.tzinfo:
    try:
        return zoneinfo.ZoneInfo(name)
    except Exception:
        return UTC


# -- the parsed spec -----------------------------------------------------------------

@dataclass(frozen=True)
class Spec:
    kind: str  # every | daily | weekly | monthly | at | cron
    text: str  # canonical rendering, used by describe()
    tz_name: str = "UTC"
    seconds: int = 0  # kind=every
    hour: int = 0
    minute: int = 0
    weekdays: tuple[int, ...] = ()  # kind=weekly, Monday=0
    day_of_month: int = 0  # kind=monthly
    at: _dt.datetime | None = None  # kind=at, naive local wall clock
    cron: tuple[frozenset[int], ...] | None = None  # (minute, hour, dom, month, dow)
    cron_dom_star: bool = True
    cron_dow_star: bool = True

    # -- introspection ---------------------------------------------------------------
    @property
    def is_one_shot(self) -> bool:
        return self.kind == "at"

    @property
    def tz(self) -> _dt.tzinfo:
        return _zone(self.tz_name)

    def describe(self) -> str:
        return self.text

    def with_tz(self, tz_name: str) -> "Spec":
        from dataclasses import replace

        return replace(self, tz_name=tz_name)

    def min_interval(self) -> _dt.timedelta | None:
        """Smallest gap this spec can produce, or None when it fires at most once.
        The scheduler compares it against `triggers.min_interval_minutes`."""
        if self.kind == "every":
            return _dt.timedelta(seconds=self.seconds)
        if self.kind == "daily":
            return _dt.timedelta(days=1)
        if self.kind == "weekly":
            days = sorted(self.weekdays)
            if len(days) == 1:
                return _dt.timedelta(days=7)
            gaps = [b - a for a, b in zip(days, days[1:])] + [days[0] + 7 - days[-1]]
            return _dt.timedelta(days=min(gaps))
        if self.kind == "monthly":
            return _dt.timedelta(days=28)
        if self.kind == "at":
            return None
        # cron: sample the next few firings and take the smallest gap.
        cursor = _dt.datetime(2026, 1, 1, tzinfo=UTC)
        times: list[_dt.datetime] = []
        for _ in range(6):
            nxt = self.next_after(cursor)
            if nxt is None:
                break
            times.append(nxt)
            cursor = nxt
        if len(times) < 2:
            return None
        return min(b - a for a, b in zip(times, times[1:]))

    # -- the one computation ---------------------------------------------------------
    def next_after(self, dt: _dt.datetime) -> _dt.datetime | None:
        """The first firing strictly after `dt`, as an aware UTC datetime (None when the
        spec has no future firing). Naive `dt` is read as UTC."""
        tz = self.tz
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        if self.kind == "every":
            return (dt + _dt.timedelta(seconds=self.seconds)).astimezone(UTC)
        local = dt.astimezone(tz).replace(tzinfo=None, microsecond=0)
        if self.kind == "at":
            assert self.at is not None
            return self._localize(self.at, tz) if self.at > local else None
        if self.kind == "daily":
            for k in range(0, 3):
                cand = self._clock(local.date() + _dt.timedelta(days=k))
                if cand > local:
                    return self._localize(cand, tz)
            return None
        if self.kind == "weekly":
            for k in range(0, 15):
                day = local.date() + _dt.timedelta(days=k)
                if day.weekday() not in self.weekdays:
                    continue
                cand = self._clock(day)
                if cand > local:
                    return self._localize(cand, tz)
            return None
        if self.kind == "monthly":
            year, month = local.year, local.month
            for _ in range(0, 14):
                if self.day_of_month <= _days_in_month(year, month):
                    cand = self._clock(_dt.date(year, month, self.day_of_month))
                    if cand > local:
                        return self._localize(cand, tz)
                year, month = (year + 1, 1) if month == 12 else (year, month + 1)
            return None
        return self._next_cron(local, tz)

    # -- helpers ---------------------------------------------------------------------
    def _clock(self, day: _dt.date) -> _dt.datetime:
        return _dt.datetime(day.year, day.month, day.day, self.hour, self.minute)

    @staticmethod
    def _localize(naive: _dt.datetime, tz: _dt.tzinfo) -> _dt.datetime:
        return naive.replace(tzinfo=tz).astimezone(UTC)

    def _next_cron(self, local: _dt.datetime, tz: _dt.tzinfo) -> _dt.datetime | None:
        assert self.cron is not None
        minutes, hours, doms, months, dows = self.cron
        cursor = local.replace(second=0, microsecond=0) + _dt.timedelta(minutes=1)
        limit = local + _dt.timedelta(days=366 * 5)
        while cursor <= limit:
            if cursor.month not in months:
                year, month = (cursor.year + 1, 1) if cursor.month == 12 else (cursor.year, cursor.month + 1)
                cursor = _dt.datetime(year, month, 1)
                continue
            if not self._cron_day_matches(cursor, doms, dows):
                cursor = _dt.datetime.combine(cursor.date() + _dt.timedelta(days=1), _dt.time())
                continue
            if cursor.hour not in hours:
                cursor = cursor.replace(minute=0) + _dt.timedelta(hours=1)
                continue
            if cursor.minute not in minutes:
                cursor = cursor + _dt.timedelta(minutes=1)
                continue
            return self._localize(cursor, tz)
        return None

    def _cron_day_matches(self, cursor: _dt.datetime, doms: frozenset[int], dows: frozenset[int]) -> bool:
        dow = (cursor.weekday() + 1) % 7  # Monday=0 → cron Sunday=0
        if self.cron_dom_star and self.cron_dow_star:
            return True
        if self.cron_dom_star:
            return dow in dows
        if self.cron_dow_star:
            return cursor.day in doms
        return cursor.day in doms or dow in dows  # cron's documented OR


def _days_in_month(year: int, month: int) -> int:
    import calendar

    return calendar.monthrange(year, month)[1]


# -- parsing --------------------------------------------------------------------------

_EVERY = re.compile(r"^every\s+(\d+)\s*([a-z]+)$")
_EVERY_ONE = re.compile(r"^every\s+([a-z]+)$")
_DAILY = re.compile(r"^daily\s+at\s+(\d{1,2}):(\d{2})$")
_WEEKLY = re.compile(r"^weekly\s+on\s+([a-z][a-z,\s]*?)\s+at\s+(\d{1,2}):(\d{2})$")
_MONTHLY = re.compile(r"^monthly\s+on\s+(\d{1,2})(?:st|nd|rd|th)?\s+at\s+(\d{1,2}):(\d{2})$")
_AT = re.compile(r"^at\s+(\d{4})-(\d{2})-(\d{2})[t\s]+(\d{1,2}):(\d{2})(?::\d{2})?$")


def parse(spec: str, tz: str = "UTC") -> Spec:
    """Parse a spec string. Raises `SpecError` with the accepted forms on anything else."""
    raw = (spec or "").strip()
    if not raw:
        raise SpecError("spec is empty")
    text = re.sub(r"\s+", " ", raw.lower()).strip()

    m = _EVERY.match(text) or _EVERY_ONE.match(text)
    if m:
        count = int(m.group(1)) if m.re is _EVERY else 1
        word = m.group(2) if m.re is _EVERY else m.group(1)
        unit = _UNIT_WORDS.get(word)
        if unit is None:
            raise SpecError(f"unknown interval unit {word!r}")
        if count < 1:
            raise SpecError("an interval must be at least 1")
        seconds = count * _UNIT_SECONDS[unit]
        return Spec(kind="every", text=f"every {count}{unit}", tz_name=tz, seconds=seconds)

    m = _DAILY.match(text)
    if m:
        hour, minute = _clock_of(m.group(1), m.group(2))
        return Spec(kind="daily", text=f"daily at {hour:02d}:{minute:02d}", tz_name=tz, hour=hour, minute=minute)

    m = _WEEKLY.match(text)
    if m:
        days = []
        for part in re.split(r"[,\s]+", m.group(1).strip()):
            if not part:
                continue
            if part not in _DAY_NAMES:
                raise SpecError(f"unknown day {part!r}")
            days.append(_DAY_NAMES[part])
        if not days:
            raise SpecError("weekly needs at least one day")
        weekdays = tuple(sorted(set(days)))
        hour, minute = _clock_of(m.group(2), m.group(3))
        label = ",".join(_DAY_SHORT[d] for d in weekdays)
        return Spec(
            kind="weekly", text=f"weekly on {label} at {hour:02d}:{minute:02d}", tz_name=tz,
            hour=hour, minute=minute, weekdays=weekdays,
        )

    m = _MONTHLY.match(text)
    if m:
        day = int(m.group(1))
        if not 1 <= day <= 31:
            raise SpecError("the day of the month must be 1-31")
        hour, minute = _clock_of(m.group(2), m.group(3))
        return Spec(
            kind="monthly", text=f"monthly on {day} at {hour:02d}:{minute:02d}", tz_name=tz,
            hour=hour, minute=minute, day_of_month=day,
        )

    m = _AT.match(text)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hour, minute = _clock_of(m.group(4), m.group(5))
        try:
            moment = _dt.datetime(year, month, day, hour, minute)
        except ValueError as exc:
            raise SpecError(f"not a real date and time: {exc}") from exc
        return Spec(kind="at", text=f"at {moment.isoformat(timespec='minutes')}", tz_name=tz, at=moment)

    fields = text.split(" ")
    if len(fields) == 5:
        return _parse_cron(fields, tz)

    raise SpecError(f"could not parse {raw!r}")


def _clock_of(hour_text: str, minute_text: str) -> tuple[int, int]:
    hour, minute = int(hour_text), int(minute_text)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise SpecError(f"{hour_text}:{minute_text} is not a time of day (00:00-23:59)")
    return hour, minute


def _parse_cron(fields: list[str], tz: str) -> Spec:
    minutes = _cron_field(fields[0], 0, 59, {}, "minute")
    hours = _cron_field(fields[1], 0, 23, {}, "hour")
    doms = _cron_field(fields[2], 1, 31, {}, "day of month")
    months = _cron_field(fields[3], 1, 12, _CRON_MON, "month")
    dows = _cron_field(fields[4], 0, 7, _CRON_DOW, "day of week")
    dows = frozenset(0 if d == 7 else d for d in dows)
    return Spec(
        kind="cron", text=" ".join(fields), tz_name=tz,
        cron=(minutes, hours, doms, months, dows),
        cron_dom_star=fields[2] == "*", cron_dow_star=fields[4] == "*",
    )


def _cron_field(text: str, lo: int, hi: int, names: dict[str, int], label: str) -> frozenset[int]:
    out: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise SpecError(f"empty {label} field in the cron spec")
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            if not step_text.isdigit() or int(step_text) < 1:
                raise SpecError(f"bad step {step_text!r} in the cron {label} field")
            step = int(step_text)
        if part in ("*", ""):
            start, end = lo, hi
        elif "-" in part.lstrip("-"):
            a, _, b = part.partition("-")
            start, end = _cron_value(a, lo, hi, names, label), _cron_value(b, lo, hi, names, label)
            if start > end:
                raise SpecError(f"reversed range {part!r} in the cron {label} field")
        else:
            start = _cron_value(part, lo, hi, names, label)
            end = hi if step > 1 else start
        out.update(range(start, end + 1, step))
    if not out:
        raise SpecError(f"the cron {label} field matches nothing")
    return frozenset(out)


def _cron_value(text: str, lo: int, hi: int, names: dict[str, int], label: str) -> int:
    text = text.strip()
    if text in names:
        return names[text]
    if not text.isdigit():
        raise SpecError(f"{text!r} is not a valid cron {label}")
    value = int(text)
    if not lo <= value <= hi:
        raise SpecError(f"cron {label} {value} is outside {lo}-{hi}")
    return value
