"""Issue-time rules for forecast entries (codebook `issued_at`, ADR 0003)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from scanner.core.codebook import IssuedAtField, IssuedAtFixedLocalTime, IssuedAtOffset, IssuedAtRule
from scanner.core.time import local_day, parse_duration, parse_source_utc


def resolve_issued_at(
    rule: IssuedAtRule | None, *, interval_start: datetime, record: dict[str, Any], tz: str
) -> datetime | None:
    if rule is None:
        return None
    if isinstance(rule, IssuedAtField):
        raw = record.get(rule.field)
        if raw is None:
            raise ValueError(f"issue-time field {rule.field} is null")
        return parse_source_utc(str(raw))
    if isinstance(rule, IssuedAtFixedLocalTime):
        day = local_day(interval_start, tz) + timedelta(days=rule.day_offset)
        return datetime.combine(day, rule.local_time, tzinfo=ZoneInfo(tz)).astimezone(UTC)
    if isinstance(rule, IssuedAtOffset):
        return interval_start + parse_duration(rule.offset)
    raise TypeError(f"unknown issued_at rule {rule!r}")
