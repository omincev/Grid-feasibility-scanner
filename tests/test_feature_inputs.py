"""Source precedence and resolution joins in feature inputs (WP3.3, scanner.features.inputs).

Synthetic frames: entity, source, series and interval columns only, values made up.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import polars as pl
import pytest

from scanner.features.inputs import InputError, check_disjoint_series, select_source

T0 = datetime(2025, 9, 30, 22, tzinfo=UTC)  # 2025-10-01 00:00 Copenhagen
H, Q = timedelta(hours=1), timedelta(minutes=15)
UTC_DT = pl.Datetime("us", "UTC")
PRECEDENCE = {"dk": {"day_ahead_price": ["energidataservice", "entsoe"]}, "nl": {"day_ahead_price": ["entsoe"]}}


def rows(entity: str, source: str, series_id: int, start: datetime, step: timedelta, n: int) -> list[dict[str, Any]]:
    return [
        {
            "entity_id": entity,
            "source": source,
            "series_id": series_id,
            "interval_start": start + i * step,
            "interval_end": start + (i + 1) * step,
            "value": float(i),
        }
        for i in range(n)
    ]


def frame(*parts: list[dict[str, Any]]) -> pl.DataFrame:
    schema = {
        "entity_id": pl.String,
        "source": pl.String,
        "series_id": pl.Int64,
        "interval_start": UTC_DT,
        "interval_end": UTC_DT,
        "value": pl.Float64,
    }
    return pl.DataFrame([r for p in parts for r in p], schema=schema)


def test_precedence_picks_one_source_per_zone() -> None:
    df = frame(
        rows("dk.zone.dk1", "energidataservice", 1, T0, Q, 96),
        rows("dk.zone.dk1", "entsoe", 2, T0 - 24 * H, H, 24),  # longer history, lower precedence
        rows("dk.zone.dk1", "entsoe", 3, T0, Q, 96),
        rows("nl.zone.nl", "entsoe", 4, T0, Q, 96),
    )
    out = select_source(df, "day_ahead_price", PRECEDENCE)
    assert out.filter(pl.col("entity_id") == "dk.zone.dk1")["source"].unique().to_list() == ["energidataservice"]
    assert out.filter(pl.col("entity_id") == "nl.zone.nl").height == 96


def test_single_source_needs_no_precedence() -> None:
    df = frame(rows("hu.zone.hu", "entsoe", 1, T0, Q, 4))
    assert select_source(df, "day_ahead_price", {}).height == 4


@pytest.mark.parametrize("precedence", [{}, {"dk": {"day_ahead_price": ["nordpool"]}}, {"dk": {"other": ["entsoe"]}}])
def test_several_sources_without_a_usable_precedence_raise(precedence: dict[str, dict[str, list[str]]]) -> None:
    df = frame(rows("dk.zone.dk1", "energidataservice", 1, T0, Q, 4), rows("dk.zone.dk1", "entsoe", 2, T0, Q, 4))
    with pytest.raises(InputError, match="precedence"):
        select_source(df, "day_ahead_price", precedence)


def test_disjoint_series_of_one_source_concatenate() -> None:
    """Hourly to 2025-09-30, quarter-hourly from 2025-10-01: one measure, two stored series (ADR 0002)."""
    df = frame(
        rows("nl.zone.nl", "entsoe", 1, T0 - 24 * H, H, 24),
        rows("nl.zone.nl", "entsoe", 2, T0, Q, 96),
        rows("dk.zone.dk1", "entsoe", 3, T0, Q, 96),  # another entity may start anywhere
    )
    check_disjoint_series(df)  # no error


def test_overlapping_series_raise() -> None:
    df = frame(rows("nl.zone.nl", "entsoe", 1, T0 - 24 * H, H, 25), rows("nl.zone.nl", "entsoe", 2, T0, Q, 96))
    with pytest.raises(InputError, match="overlap"):
        check_disjoint_series(df)
