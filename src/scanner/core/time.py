"""Time conventions (ADR 0001, 0002).

All stored times are timezone-aware UTC. Values cover half-open intervals [start, end).
Local time is used only to define local days (partitions, completeness checks) and in display code.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import polars as pl

_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)

Resample = Literal["sum", "mean", "first", "last", "none"]


class TimeConventionError(ValueError):
    """Raised when a timestamp or duration cannot be interpreted without guessing."""


def parse_duration(value: str) -> timedelta:
    """Parse the ISO 8601 durations used in manifests: P1D, PT15M, PT1H, P3D, PT26H, -PT5H."""
    sign = 1
    if value.startswith("-"):
        sign, value = -1, value[1:]
    m = _DURATION_RE.match(value)
    if not m or value in ("P", "PT"):
        raise TimeConventionError(f"unsupported duration: {value!r}")
    parts = {k: int(v) for k, v in m.groupdict().items() if v is not None}
    return sign * timedelta(
        days=parts.get("days", 0),
        hours=parts.get("hours", 0),
        minutes=parts.get("minutes", 0),
        seconds=parts.get("seconds", 0),
    )


def format_duration(td: timedelta) -> str:
    total = int(td.total_seconds())
    sign = "-" if total < 0 else ""
    total = abs(total)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    out = "P" + (f"{days}D" if days else "")
    clock = (f"{hours}H" if hours else "") + (f"{minutes}M" if minutes else "") + (f"{seconds}S" if seconds else "")
    if clock:
        out += "T" + clock
    return sign + (out if out != "P" else "PT0S")


def ensure_utc(ts: datetime) -> datetime:
    """Reject naive datetimes; convert aware ones to UTC."""
    if ts.tzinfo is None:
        raise TimeConventionError(f"naive datetime not allowed: {ts.isoformat()}")
    return ts.astimezone(UTC)


def parse_source_utc(value: str) -> datetime:
    """Parse a source timestamp that the dossier documents as UTC, with or without a 'Z' suffix.

    Energi Data Service returns e.g. '2025-10-01T00:00:00' for its *UTC fields; the meaning comes
    from the field name, which the manifest declares. Offsets other than UTC are rejected.
    """
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1]
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is not None:
        if dt.utcoffset() != timedelta(0):
            raise TimeConventionError(f"expected UTC, got offset {dt.utcoffset()} in {value!r}")
        return dt.astimezone(UTC)
    return dt.replace(tzinfo=UTC)


def local_day_bounds(day: date, tz: str) -> tuple[datetime, datetime]:
    """UTC start and end of a local calendar day."""
    zone = ZoneInfo(tz)
    start = datetime.combine(day, time(0), tzinfo=zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=zone).astimezone(UTC)
    return start, end


def local_day(ts: datetime, tz: str) -> date:
    return ensure_utc(ts).astimezone(ZoneInfo(tz)).date()


def iter_intervals(start: datetime, end: datetime, resolution: timedelta) -> Iterator[tuple[datetime, datetime]]:
    start, end = ensure_utc(start), ensure_utc(end)
    if resolution <= timedelta(0):
        raise TimeConventionError("resolution must be positive")
    t = start
    while t < end:
        yield t, t + resolution
        t += resolution


def expected_intervals(day: date, tz: str, resolution: timedelta) -> list[tuple[datetime, datetime]]:
    """Intervals a complete local day must contain. A Danish day has 92, 96 or 100 quarter-hours."""
    if resolution > timedelta(hours=1) or (timedelta(hours=1) % resolution) != timedelta(0):
        raise TimeConventionError("expected_intervals supports sub-hourly resolutions dividing one hour")
    start, end = local_day_bounds(day, tz)
    return list(iter_intervals(start, end, resolution))


def expected_count(day: date, tz: str, resolution: timedelta) -> int:
    return len(expected_intervals(day, tz, resolution))


def resample(df: pl.DataFrame, target: timedelta, how: Resample) -> pl.DataFrame:
    """Resample a native-resolution frame to a coarser sub-daily resolution.

    Input columns: interval_start (UTC datetime), interval_end, value, plus optional key columns
    listed in group columns (all non-time, non-value columns). Output adds `n` (number of source
    intervals per bucket) so callers can drop incomplete buckets. Daily aggregation must use local
    days and is not done here.
    """
    if how == "none":
        raise TimeConventionError("this measure must not be resampled (codebook resample: none)")
    if target > timedelta(hours=1) or (timedelta(hours=1) % target) != timedelta(0):
        raise TimeConventionError("resample supports targets that divide one hour; use local days for daily")
    keys = [c for c in df.columns if c not in ("interval_start", "interval_end", "value")]
    every = f"{int(target.total_seconds())}s"
    agg = {
        "sum": pl.col("value").sum(),
        "mean": pl.col("value").mean(),
        "first": pl.col("value").sort_by("interval_start").first(),
        "last": pl.col("value").sort_by("interval_start").last(),
    }[how]
    out = (
        df.with_columns(pl.col("interval_start").dt.truncate(every).alias("bucket"))
        .group_by([*keys, "bucket"])
        .agg(agg.alias("value"), pl.len().alias("n"))
        .rename({"bucket": "interval_start"})
        .with_columns(
            (pl.col("interval_start") + pl.duration(seconds=int(target.total_seconds()))).alias("interval_end")
        )
        .sort([*keys, "interval_start"])
    )
    return out.select([*keys, "interval_start", "interval_end", "value", "n"])
