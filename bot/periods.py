"""Kyiv calendar boundaries converted to UTC, including 23/25-hour DST days."""
from datetime import datetime, time, timedelta, timezone as tz
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    return datetime.now(tz.utc)


def period_bounds(now: datetime, timezone: str = 'Europe/Kyiv', payout_time: str = '18:00'):
    if now.tzinfo is None:
        raise ValueError('An aware datetime is required')
    zone = ZoneInfo(timezone)
    local = now.astimezone(zone)
    boundary = datetime.combine(local.date(), time.fromisoformat(payout_time), zone)
    if local < boundary:
        boundary = datetime.combine(local.date() - timedelta(days=1), time.fromisoformat(payout_time), zone)
    end = datetime.combine(boundary.date() + timedelta(days=1), time.fromisoformat(payout_time), zone)
    return boundary.astimezone(tz.utc), end.astimezone(tz.utc)


def iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError('An aware datetime is required')
    return value.astimezone(tz.utc).isoformat()
