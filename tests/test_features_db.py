"""Feature runs against a migrated database. Run with: TEST_DATABASE_URL=... pytest -m db

Inserts minimal series for two test-only zones standing in for DK1 and DK2 (test series keys and zones,
removed afterwards), runs run_features over a window ending 2025-10-03 on a repository root that lists only
those zones, and checks the rows in feat.feature_value and ops.compute_run. Series the pipeline wrote for
the real zones are never read.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from conftest import zones_root

from scanner.core import db
from scanner.core.time import local_day_bounds
from scanner.features import run as features_run
from scanner.features.run import run_features

pytestmark = pytest.mark.db

CPH = "Europe/Copenhagen"
Q, H = timedelta(minutes=15), timedelta(hours=1)
END_DAY = date(2025, 10, 3)
WINDOW = (datetime(2025, 3, 3, 23, tzinfo=UTC), datetime(2025, 10, 2, 22, tzinfo=UTC))
SEP30, OCT1 = local_day_bounds(date(2025, 9, 30), CPH)[0], local_day_bounds(date(2025, 10, 1), CPH)[0]
MAR18, MAR19 = local_day_bounds(date(2025, 3, 18), CPH)[0], local_day_bounds(date(2025, 3, 19), CPH)[0]
PREFIX = "test.features."
ZONE = {"dk1": "dk.zone.test_features_dk1", "dk2": "dk.zone.test_features_dk2"}

# entry_id: (class, kind, unit, resample)
ENTRIES = {
    "dk.eds.Elspotprices.SpotPriceEUR": ("day_ahead_price", "actual", "EUR/MWh", "mean"),
    "dk.eds.DayAheadPrices.DayAheadPriceEUR": ("day_ahead_price", "actual", "EUR/MWh", "mean"),
    "dk.eds.ImbalancePrice.SpotPriceEUR": ("day_ahead_price", "actual", "EUR/MWh", "mean"),
    "dk.eds.ImbalancePrice.ImbalancePriceEUR": ("imbalance_price.nordic_single", "settlement", "EUR/MWh", "mean"),
    "dk.eds.ImbalancePrice.DominatingDirection": ("balancing.dominating_direction", "settlement", "1", "none"),
    **{
        f"dk.eds.Forecasts_Hour.ForecastDayAhead.{t}": (f"generation_forecast.{t}", "forecast", "MWh", "sum")
        for t in ("solar", "wind_onshore", "wind_offshore")
    },
    **{
        f"dk.test.Actuals.{t}": (f"generation_actual.{t}", "actual", "MWh", "sum")
        for t in ("solar", "wind_onshore", "wind_offshore")
    },
    "dk.test.Actuals.consumption": ("consumption_actual", "actual", "MWh", "sum"),
}


@pytest.fixture()
def db_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[str, list[str], Path]]:
    """The test database URL, a list the test fills with run IDs to delete afterwards, and the repository
    root listing the test zones (whose inter-zone pair replaces DK1-DK2)."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    monkeypatch.setenv("GIT_SHA", "testsha")
    monkeypatch.setattr(features_run, "INTERZONE_PAIRS", ((ZONE["dk1"], ZONE["dk2"]),))
    with psycopg.connect(url) as conn:
        _cleanup(conn, [])
        _reference(conn)
        conn.commit()
    run_ids: list[str] = []
    yield url, run_ids, zones_root(tmp_path, list(ZONE.values()))
    with psycopg.connect(url) as conn:
        _cleanup(conn, run_ids)


def _cleanup(conn: psycopg.Connection[Any], run_ids: list[str]) -> None:
    ids = [uuid.UUID(r) for r in run_ids]
    zones = list(ZONE.values())
    conn.execute("delete from feat.feature_value where run_id = any(%s) or entity_id = any(%s)", (ids, zones))
    conn.execute("delete from ops.compute_run where run_id = any(%s)", (ids,))
    keys = f"{PREFIX}%"
    conn.execute(
        "delete from core.observation where series_id in (select series_id from core.series where series_key like %s)",
        (keys,),
    )
    conn.execute("delete from core.series where series_key like %s", (keys,))
    conn.execute("delete from ref.entity where entity_id = any(%s)", (zones,))
    conn.execute("delete from ref.codebook_entry where entry_id like 'dk.test.%%' and source_path = 't'")
    conn.commit()


def _reference(conn: psycopg.Connection[Any]) -> None:
    conn.execute(
        "insert into ref.country (code, name, status, timezone, currency, config, source_path, content_hash) "
        "values ('dk','Denmark','active','Europe/Copenhagen','DKK','{}','t','t') on conflict do nothing"
    )
    for zone, entity_id in ZONE.items():
        conn.execute(
            "insert into ref.entity (entity_id, kind, country_code, name) values (%s, 'zone', 'dk', %s) "
            "on conflict do nothing",
            (entity_id, zone.upper()),
        )
    for entry_id, (cls, kind, unit, how) in ENTRIES.items():
        conn.execute("insert into ref.equivalence_class values (%s, 'x', 'x') on conflict do nothing", (cls,))
        source, dataset, field = entry_id.split(".")[1:4]
        conn.execute(
            "insert into ref.codebook_entry (entry_id, source, dataset, country_code, field, equivalence_class, "
            "kind, unit, resample, definition, status, source_path, content_hash) "
            "values (%s, %s, %s, 'dk', %s, %s, %s, %s, %s, '{}', 'draft', 't', 't') on conflict do nothing",
            (entry_id, source, dataset, field, cls, kind, unit, how),
        )


def _write(
    conn: psycopg.Connection[Any],
    zone: str,
    entry_id: str,
    start: datetime,
    step: timedelta,
    values: list[float | None],
    issued_at: datetime | None = None,
) -> None:
    _, kind, unit, _ = ENTRIES[entry_id]
    key = f"{PREFIX}{zone}.{entry_id}"
    sid = db.ensure_series(conn, db.SeriesSpec(key, entry_id, ZONE[zone], unit, step, kind, "public", None))
    rows = [
        db.ObservationRow(key, start + i * step, start + (i + 1) * step, issued_at, v) for i, v in enumerate(values)
    ]
    db.write_observations(conn, rows, {key: sid}, uuid.uuid4())
    conn.commit()


def _insert_inputs(conn: psycopg.Connection[Any]) -> None:
    dk1_hourly = [float(i - 3) for i in range(24)]  # -3 .. 20: four non-positive hours, spread 23
    dk2_hourly = [p - 15 if i < 12 else p for i, p in enumerate(dk1_hourly)]  # spread 38
    _write(conn, "dk1", "dk.eds.Elspotprices.SpotPriceEUR", SEP30, H, [*dk1_hourly, 999.0])  # 999: after the switch
    _write(conn, "dk2", "dk.eds.Elspotprices.SpotPriceEUR", SEP30, H, dk2_hourly)
    _write(conn, "dk1", "dk.eds.DayAheadPrices.DayAheadPriceEUR", OCT1, Q, [50.0] * 40 + [150.0] + [50.0] * 55)
    _write(conn, "dk2", "dk.eds.DayAheadPrices.DayAheadPriceEUR", OCT1, Q, [50.0] * 96)

    _write(conn, "dk1", "dk.eds.ImbalancePrice.DominatingDirection", MAR18, Q, [1.0] * 96 + [-1.0] * 96)
    _write(conn, "dk1", "dk.eds.ImbalancePrice.SpotPriceEUR", MAR18, Q, [50.0] * 192)
    _write(conn, "dk1", "dk.eds.ImbalancePrice.ImbalancePriceEUR", MAR18, Q, [60.0] * 96 + [None] + [20.0] * 95)

    issued = SEP30 - timedelta(hours=6)  # 18:00 local the day before
    for tech, mwh in (("solar", 0.0), ("wind_onshore", 100.0), ("wind_offshore", 50.0)):
        entry = f"dk.eds.Forecasts_Hour.ForecastDayAhead.{tech}"
        _write(conn, "dk1", entry, SEP30, H, [mwh] * 24, issued)
        _write(conn, "dk1", entry, SEP30, H, [999.0] * 24, SEP30 + timedelta(days=2))  # issued too late
    for tech, mwh in (("solar", 0.0), ("wind_onshore", 20.0), ("wind_offshore", 10.0)):
        _write(conn, "dk1", f"dk.test.Actuals.{tech}", SEP30, Q, [mwh] * 96)


def _values(conn: psycopg.Connection[Any], run_id: str, feature_key: str, statistic: str, entity: str) -> list[Any]:
    return conn.execute(
        "select period_start, period_end, value from feat.feature_value "
        "where run_id = %s and feature_key = %s and statistic = %s and entity_id = %s "
        "order by period_start, period_end",
        (run_id, feature_key, statistic, ZONE[entity]),
    ).fetchall()


def _whole(conn: psycopg.Connection[Any], run_id: str, feature_key: str, statistic: str, entity: str = "dk1") -> float:
    rows = [v for s, e, v in _values(conn, run_id, feature_key, statistic, entity) if (s, e) == WINDOW]
    assert len(rows) == 1, (feature_key, statistic, rows)
    return float(rows[0])


def test_run_features_writes_traceable_rows(db_runs: tuple[str, list[str], Path]) -> None:
    url, run_ids, root = db_runs
    with psycopg.connect(url) as conn:
        _insert_inputs(conn)
        before = run_features(["f4_surplus"], database_url=url, end_day=END_DAY, root=root)
        run_ids += [o.run_id for o in before]
        assert before[0].status == "skipped" and before[0].rows_written == 0
        _write(conn, "dk1", "dk.test.Actuals.consumption", SEP30, Q, [25.0] * 96)

        outcomes = {o.feature: o for o in run_features(database_url=url, end_day=END_DAY, root=root)}
        run_ids += [o.run_id for o in outcomes.values()]
        assert {k: o.status for k, o in outcomes.items()} == dict.fromkeys(outcomes, "ok")

        runs = conn.execute(
            "select run_id::text, kind, name, status, code_version, error, params->'window' from ops.compute_run "
            "where run_id = any(%s)",
            ([uuid.UUID(o.run_id) for o in [*before, *outcomes.values()]],),
        ).fetchall()
        by_id = {r[0]: r for r in runs}
        assert by_id[before[0].run_id][3] == "skipped" and "consumption" in by_id[before[0].run_id][5]
        for o in outcomes.values():
            _, kind, name, status, version, error, window = by_id[o.run_id]
            assert (kind, name, status, version, error) == ("feature", o.feature, "ok", "testsha", None)
            assert window == [WINDOW[0].isoformat(), WINDOW[1].isoformat()]
        with pytest.raises(psycopg.errors.ForeignKeyViolation):  # every run_id must resolve to a run row
            conn.execute(
                "insert into feat.feature_value (feature_key, entity_id, period_start, period_end, value, run_id, "
                "code_version) values ('x', 'dk.zone.dk1', now(), now(), 1, %s, 'x')",
                (uuid.uuid4(),),
            )
        conn.rollback()

        f1 = outcomes["f1_price_spread"].run_id
        assert [v for _, _, v in _values(conn, f1, "f1_intraday_spread_native", "daily_spread", "dk1")] == [23.0, 100.0]
        assert [v for _, _, v in _values(conn, f1, "f1_intraday_spread_native", "daily_spread", "dk2")] == [38.0, 0.0]
        daily = _values(conn, f1, "f1_intraday_spread_hourly", "daily_spread", "dk1")
        assert [(s, e) for s, e, _ in daily] == [(SEP30, OCT1), (OCT1, OCT1 + timedelta(hours=24))]
        assert [v for _, _, v in daily] == [23.0, 25.0]
        # whole window, then the sub-windows split at the 2025-10-01 resolution switch
        means = _values(conn, f1, "f1_intraday_spread_native", "mean", "dk1")
        assert [(s, e, v) for s, e, v in means] == [
            (WINDOW[0], OCT1, 23.0),
            (WINDOW[0], WINDOW[1], 61.5),
            (OCT1, WINDOW[1], 100.0),
        ]
        share = (12 * 3600 + 900) / (2 * 86400)
        assert _whole(conn, f1, "f1_interzone_spread", "share_above_10", "dk1") == pytest.approx(share)
        assert _whole(conn, f1, "f1_interzone_spread", "share_above_10", "dk2") == pytest.approx(share)
        sens = conn.execute("select distinct sensitivity::text from feat.feature_value where run_id = %s", (f1,))
        assert sens.fetchall() == [("public",)]

        f2 = outcomes["f2_curtailment"].run_id
        assert _whole(conn, f2, "f2_nonpositive_price_share", "share") == pytest.approx(4 / 48)
        assert _whole(conn, f2, "f2_curtailment_proxy_mwh", "sum") == 4 * (150.0 - 120.0)
        assert _values(conn, f2, "f2_curtailment_proxy_mwh", "sum", "dk2") == []

        f4 = outcomes["f4_surplus"].run_id
        assert _whole(conn, f4, "f4_surplus_index", "mean") == pytest.approx(0.2)
        assert _whole(conn, f4, "f4_surplus_index", "n_intervals") == 96.0

        f5 = outcomes["f5_balancing_activation"].run_id
        up = _values(conn, f5, "f5_activation_share_up", "share", "dk1")
        assert [(s, e, v) for s, e, v in up] == [
            (WINDOW[0], MAR19, 1.0),
            (WINDOW[0], WINDOW[1], 0.5),
            (MAR19, WINDOW[1], 0.0),
        ]
        assert _whole(conn, f5, "f5_imbalance_spot_spread", "mean") == pytest.approx((96 * 10 + 95 * 30) / 191)


def test_feature_runs_need_only_the_analytics_role(db_runs: tuple[str, list[str], Path], analytics_url: str) -> None:
    """The analytics location may connect as a login role in scanner_analytics (ANALYTICS_DATABASE_URL)."""
    url, run_ids, root = db_runs
    with psycopg.connect(url) as conn:
        _insert_inputs(conn)
    outcomes = run_features(
        ["f1_price_spread", "f5_balancing_activation"], database_url=analytics_url, end_day=END_DAY, root=root
    )
    run_ids += [o.run_id for o in outcomes]
    assert [(o.status, o.rows_written > 0) for o in outcomes] == [("ok", True)] * 2
