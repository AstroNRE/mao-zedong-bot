from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from bot.periods import period_bounds

KYIV = ZoneInfo("Europe/Kyiv")


def test_boundary_belongs_to_next_period():
    boundary = datetime(2026, 10, 30, 18, tzinfo=KYIV)
    old_start, old_end = period_bounds(boundary - timedelta(microseconds=1))
    new_start, new_end = period_bounds(boundary)
    assert old_end == new_start == boundary.astimezone(timezone.utc)
    assert old_start < new_start < new_end


@pytest.mark.parametrize("date,hours", [((2026, 3, 29), 23), ((2026, 10, 25), 25)])
def test_dst_period_duration(date, hours):
    now = datetime(*date, 17, tzinfo=KYIV)
    start, end = period_bounds(now)
    assert (end - start).total_seconds() == hours * 3600
    assert start.astimezone(KYIV).hour == end.astimezone(KYIV).hour == 18


def test_periods_are_aware_utc():
    start, end = period_bounds(datetime(2026, 10, 30, 10, tzinfo=timezone.utc))
    assert start.utcoffset() == end.utcoffset() == timedelta(0)
