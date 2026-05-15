"""Time helpers - all timestamps in the app are timezone-aware UTC."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def utcnow() -> datetime:
    """Return the current UTC time, timezone-aware."""
    return datetime.now(tz=UTC)


def to_utc(ts: datetime) -> datetime:
    """Coerce a datetime to UTC. Naive datetimes are assumed UTC."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def floor_to_minute(ts: datetime, minutes: int = 1) -> datetime:
    """Floor a timestamp down to the nearest ``minutes`` boundary."""
    ts = to_utc(ts)
    discard = timedelta(
        minutes=ts.minute % minutes,
        seconds=ts.second,
        microseconds=ts.microsecond,
    )
    return ts - discard
