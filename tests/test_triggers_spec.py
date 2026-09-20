"""The schedule spec grammar (03 §11.1). No app, no database: pure parsing and time."""

from __future__ import annotations

import datetime as _dt

import pytest

from loom.triggers.spec import UTC, SpecError, parse

TZ = "Europe/Amsterdam"


def utc(text: str) -> _dt.datetime:
    return _dt.datetime.fromisoformat(text).replace(tzinfo=UTC)


def nxt(spec: str, now: str, tz: str = TZ) -> str | None:
    result = parse(spec, tz=tz).next_after(utc(now))
    return None if result is None else result.isoformat().replace("+00:00", "Z")


# -- every ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "spec, canonical, seconds",
    [
        ("every 15m", "every 15m", 900),
        ("EVERY  2H", "every 2h", 7200),
        ("every 90 minutes", "every 90m", 5400),
        ("every day", "every 1d", 86400),
        ("every 30s", "every 30s", 30),
    ],
)
def test_every_parses_every_spelling(spec, canonical, seconds):
    parsed = parse(spec, tz=TZ)
    assert parsed.describe() == canonical
    assert parsed.min_interval() == _dt.timedelta(seconds=seconds)
    assert not parsed.is_one_shot
    assert parsed.next_after(utc("2026-09-20T10:00:00")) == utc("2026-09-20T10:00:00") + _dt.timedelta(seconds=seconds)


# -- clock forms ---------------------------------------------------------------------

def test_daily_at_is_the_next_local_nine_am():
    assert nxt("daily at 09:00", "2026-09-20T05:00:00") == "2026-09-20T07:00:00Z"  # 09:00 CEST
    assert nxt("daily at 09:00", "2026-09-20T08:00:00") == "2026-09-21T07:00:00Z"


def test_daily_at_keeps_the_local_hour_across_a_dst_change():
    """CEST (+2) ends on 2026-10-25; 09:00 local is 07:00Z before and 08:00Z after."""
    assert nxt("daily at 09:00", "2026-10-24T08:00:00") == "2026-10-25T08:00:00Z"
    assert nxt("daily at 09:00", "2026-10-25T09:00:00") == "2026-10-26T08:00:00Z"


def test_weekly_takes_the_soonest_named_day():
    parsed = parse("weekly on mon,thursday at 18:30", tz=TZ)
    assert parsed.describe() == "weekly on mon,thu at 18:30"
    assert parsed.weekdays == (0, 3)
    # 2026-09-20 is a Sunday → Monday 18:30 CEST = 16:30Z
    assert nxt("weekly on mon,thu at 18:30", "2026-09-20T12:00:00") == "2026-09-21T16:30:00Z"
    # from Monday evening → Thursday
    assert nxt("weekly on mon,thu at 18:30", "2026-09-21T17:00:00") == "2026-09-24T16:30:00Z"
    assert parsed.min_interval() == _dt.timedelta(days=3)


def test_monthly_skips_months_without_that_day():
    assert nxt("monthly on 15 at 08:00", "2026-09-20T00:00:00") == "2026-10-15T06:00:00Z"
    assert nxt("monthly on 31 at 08:00", "2026-01-31T12:00:00") == "2026-03-31T06:00:00Z"


def test_one_shot_fires_once_and_then_never():
    parsed = parse("at 2026-09-26T18:00", tz=TZ)
    assert parsed.is_one_shot and parsed.min_interval() is None
    assert parsed.next_after(utc("2026-09-20T10:00:00")) == utc("2026-09-26T16:00:00")
    assert parsed.next_after(utc("2026-09-27T10:00:00")) is None
    assert parse("at 2026-09-26 18:00", tz=TZ).at == parsed.at


# -- cron ----------------------------------------------------------------------------

def test_cron_weekday_range_and_list():
    # 09:00 Mon-Fri, local. 2026-09-20 is a Sunday → Monday 09:00 CEST = 07:00Z.
    assert nxt("0 9 * * 1-5", "2026-09-20T12:00:00") == "2026-09-21T07:00:00Z"
    assert nxt("0 9 * * mon,fri", "2026-09-21T08:00:00") == "2026-09-25T07:00:00Z"


def test_cron_steps():
    parsed = parse("*/15 * * * *", tz=TZ)
    assert parsed.min_interval() == _dt.timedelta(minutes=15)
    assert nxt("*/15 * * * *", "2026-09-20T10:07:00") == "2026-09-20T10:15:00Z"
    # 10:07Z is 12:07 local; the next local hour divisible by 6 is 18:00 = 16:00Z
    assert nxt("0 */6 * * *", "2026-09-20T10:07:00") == "2026-09-20T16:00:00Z"


def test_cron_day_of_month_and_month_fields():
    assert nxt("30 8 1 jan *", "2026-09-20T10:00:00") == "2027-01-01T07:30:00Z"  # CET in January


# -- errors --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "spec",
    ["", "sometimes", "every 5 fortnights", "daily at 25:00", "weekly on funday at 09:00",
     "monthly on 44 at 08:00", "at 2026-02-30T10:00", "0 9 * *", "0 99 * * *", "*/0 * * * *"],
)
def test_bad_specs_raise_with_the_accepted_forms(spec):
    with pytest.raises(SpecError) as exc:
        parse(spec, tz=TZ)
    assert "accepted forms" in str(exc.value)
