"""Zone features F1, F2, F4, F5 on synthetic frames (docs/features.md): DST days, the 2025-10-01
resolution switch, explicit nulls, negative prices and regime sub-windows."""

from __future__ import annotations

import re
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from scanner.core.registry import MarketRulesFile, load_assumptions, load_market_rules
from scanner.core.time import expected_intervals, local_day_bounds
from scanner.features import substation, zone
from scanner.features.inputs import (
    InputError,
    analysis_window,
    periods,
    regime_breaks,
    select_by_resolution_rule,
    select_vintage,
)
from scanner.features.registry import (
    FEATURE_INPUTS,
    FEATURES,
    InputSpec,
)
from scanner.features.run import RUNNERS, attach_sensitivity
from scanner.runtime import provenance
from scanner.scores.method import parse_score_method

CPH = "Europe/Copenhagen"
Q = timedelta(minutes=15)
H = timedelta(hours=1)
UTC_DT = pl.Datetime("us", "UTC")
SCHEMA: dict[str, Any] = {"entity_id": pl.String, "interval_start": UTC_DT, "interval_end": UTC_DT, "value": pl.Float64}


def series(entity: str, start: datetime, step: timedelta, values: list[float | None], **cols: Any) -> pl.DataFrame:
    rows = [(entity, start + i * step, start + (i + 1) * step, v) for i, v in enumerate(values)]
    df = pl.DataFrame(rows, schema=SCHEMA, orient="row")
    return df.with_columns(**{k: pl.lit(v) for k, v in cols.items()})


def day_start(d: date) -> datetime:
    return local_day_bounds(d, CPH)[0]


def n_intervals(d: date, step: timedelta) -> int:
    return len(expected_intervals(d, CPH, step))


def pick(rows: pl.DataFrame, feature_key: str, statistic: str, entity: str = "dk.zone.dk1") -> pl.DataFrame:
    return rows.filter(
        (pl.col("feature_key") == feature_key) & (pl.col("statistic") == statistic) & (pl.col("entity_id") == entity)
    ).sort("period_start", "period_end")


def window_for(*days: date) -> list[tuple[datetime, datetime]]:
    return [(day_start(min(days)), local_day_bounds(max(days), CPH)[1])]


@pytest.fixture(scope="module")
def dk_rules(root: Path) -> MarketRulesFile:
    return load_market_rules(root)["dk"]


# --- windows and regimes ---------------------------------------------------------------------------


def test_analysis_window_is_local_days_in_utc() -> None:
    start, end = analysis_window(CPH, date(2026, 10, 18))
    assert start == datetime(2025, 3, 3, 23, tzinfo=UTC)
    assert end == datetime(2026, 10, 17, 22, tzinfo=UTC)  # through 2026-10-17, the latest complete day


def test_regime_breaks_come_from_market_rules(dk_rules: MarketRulesFile) -> None:
    start, end = analysis_window(CPH, date(2026, 10, 18))
    f5 = regime_breaks(dk_rules, FEATURES["f5_balancing_activation"].regime_rules, CPH, start, end)
    assert f5 == [datetime(2025, 3, 18, 23, tzinfo=UTC), datetime(2026, 3, 3, 23, tzinfo=UTC)]
    f1 = regime_breaks(dk_rules, FEATURES["f1_price_spread"].regime_rules, CPH, start, end)
    assert f1 == [datetime(2025, 9, 30, 22, tzinfo=UTC)]
    assert regime_breaks(dk_rules, FEATURES["f4_surplus"].regime_rules, CPH, start, end) == []
    pers = periods(start, end, f5)
    assert pers[0] == (start, end)
    assert pers[1:] == [(start, f5[0]), (f5[0], f5[1]), (f5[1], end)]
    assert periods(start, end, []) == [(start, end)]


# --- input selection -------------------------------------------------------------------------------


def test_resolution_rule_joins_the_two_day_ahead_series(dk_rules: MarketRulesFile) -> None:
    sep30, oct1 = day_start(date(2025, 9, 30)), day_start(date(2025, 10, 1))
    df = pl.concat(
        [
            series("dk.zone.dk1", sep30, H, [1.0] * 24, entry_id="elspot"),
            series("dk.zone.dk1", oct1, H, [9.0] * 2, entry_id="elspot"),  # stray hourly after the switch
            series("dk.zone.dk1", oct1, Q, [2.0] * 96, entry_id="dayahead"),
            series("dk.zone.dk1", sep30, Q, [9.0] * 4, entry_id="other_pt15m"),  # quarter-hourly before it
        ]
    )
    out = select_by_resolution_rule(df, dk_rules, "day_ahead.resolution", CPH)
    assert out.filter(pl.col("interval_start") < oct1)["entry_id"].unique().to_list() == ["elspot"]
    assert out.filter(pl.col("interval_start") >= oct1)["entry_id"].unique().to_list() == ["dayahead"]
    assert out.height == 24 + 96


def test_vintage_latest_issued_before_start() -> None:
    t0 = datetime(2025, 9, 30, 10, tzinfo=UTC)
    base = series("dk.zone.dk1", t0, H, [1.0, 2.0, 3.0], series_id=1)
    issued = [t0 - timedelta(hours=20), t0 - timedelta(hours=2), t0 + timedelta(minutes=30)]
    df = base.with_columns(issued_at=pl.Series(issued, dtype=UTC_DT), interval_start=pl.lit(t0, dtype=UTC_DT))
    spec = InputSpec("generation_forecast.solar", vintage="latest_issued_before_start")
    out = select_vintage(df, spec)
    assert out["value"].to_list() == [2.0]
    with pytest.raises(InputError):
        select_vintage(df, InputSpec("generation_forecast.solar"))
    actuals = base.with_columns(issued_at=pl.lit(None, dtype=UTC_DT))
    assert select_vintage(actuals, InputSpec("generation_actual.solar")).height == 3


# --- F1 --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("d", "quarters"), [(date(2025, 10, 26), 100), (date(2026, 3, 29), 92), (date(2025, 11, 12), 96)]
)
def test_daily_spread_on_dst_days(d: date, quarters: int) -> None:
    assert n_intervals(d, Q) == quarters
    prices = series("dk.zone.dk1", day_start(d), Q, [float(i) for i in range(quarters)])
    rows = zone.f1_intraday_spread_native(prices, CPH, window_for(d))
    daily = pick(rows, "f1_intraday_spread_native", "daily_spread")
    assert daily["value"].to_list() == [quarters - 1.0]
    assert daily["period_start"].to_list() == [day_start(d)]
    assert daily["period_end"].to_list() == [local_day_bounds(d, CPH)[1]]
    hourly = pick(
        zone.f1_intraday_spread_hourly(prices, "mean", CPH, window_for(d)), "f1_intraday_spread_hourly", "daily_spread"
    )
    # hour means of 0..3, 4..7, ...: first 1.5, last quarters - 2.5
    assert hourly["value"].to_list() == [quarters - 4.0]


def test_daily_spread_leaves_out_days_with_nulls_or_gaps() -> None:
    d1, d2, d3 = date(2025, 11, 10), date(2025, 11, 11), date(2025, 11, 12)
    full = series("dk.zone.dk1", day_start(d1), Q, [1.0] * 95 + [5.0])
    with_null = series("dk.zone.dk1", day_start(d2), Q, [1.0] * 95 + [None])
    with_gap = series("dk.zone.dk1", day_start(d3), Q, [1.0] * 94 + [9.0])  # 95 of 96 quarters
    rows = zone.f1_intraday_spread_native(pl.concat([full, with_null, with_gap]), CPH, window_for(d1, d3))
    daily = pick(rows, "f1_intraday_spread_native", "daily_spread")
    assert daily["period_start"].to_list() == [day_start(d1)]
    assert pick(rows, "f1_intraday_spread_native", "n_days")["value"].to_list() == [1.0]


def test_hourly_resample_drops_hours_with_a_null_quarter() -> None:
    t0 = day_start(date(2025, 11, 12))
    df = series("dk.zone.dk1", t0, Q, [1.0, 2.0, 3.0, 4.0, 5.0, None, 7.0, 8.0])
    out = zone.to_resolution(df, "mean", H)
    assert out["interval_start"].to_list() == [t0]
    assert out["value"].to_list() == [2.5]


def test_f1_across_the_resolution_switch() -> None:
    sep30, oct1 = date(2025, 9, 30), date(2025, 10, 1)
    hourly = series("dk.zone.dk1", day_start(sep30), H, [10.0 + i for i in range(24)])  # spread 23
    quarters = [0.0] * 96
    quarters[40] = 100.0  # one spike: native spread 100, hourly spread 25
    prices = pl.concat([hourly, series("dk.zone.dk1", day_start(oct1), Q, quarters)])
    pers = periods(day_start(sep30), day_start(date(2025, 10, 2)), [day_start(oct1)])
    native = zone.f1_intraday_spread_native(prices, CPH, pers)
    assert pick(native, "f1_intraday_spread_native", "daily_spread")["value"].to_list() == [23.0, 100.0]
    means = pick(native, "f1_intraday_spread_native", "mean")
    assert means["value"].to_list() == [23.0, 61.5, 100.0]  # before the switch, whole window, after it
    p90 = pick(native, "f1_intraday_spread_native", "p90")["value"].to_list()[1]
    assert p90 == pytest.approx(23.0 + 0.9 * 77.0)
    hourly_rows = zone.f1_intraday_spread_hourly(prices, "mean", CPH, pers)
    assert pick(hourly_rows, "f1_intraday_spread_hourly", "daily_spread")["value"].to_list() == [23.0, 25.0]


def test_interzone_spread_weights_by_duration_and_writes_both_zones() -> None:
    t0 = day_start(date(2025, 9, 30)) + timedelta(hours=23)  # last hour before the switch
    dk1 = pl.concat([series("dk.zone.dk1", t0, H, [30.0]), series("dk.zone.dk1", t0 + H, Q, [5.0, 5.0, 5.0, None])])
    dk2 = pl.concat([series("dk.zone.dk2", t0, H, [10.0]), series("dk.zone.dk2", t0 + H, Q, [5.0, 4.0, 5.0, 0.0])])
    pers = [(t0, t0 + 2 * H)]
    rows = zone.f1_interzone_spread(pl.concat([dk1, dk2]), [("dk.zone.dk1", "dk.zone.dk2")], CPH, pers)
    for e in ("dk.zone.dk1", "dk.zone.dk2"):
        window = pick(rows, "f1_interzone_spread", "share_above_10", e).filter(pl.col("period_start") == t0)
        # diff 20 for one hour, then 0, 1, 0 for three quarters (the null quarter is not evaluated)
        assert window["value"].to_list() == [pytest.approx(3600 / (3600 + 3 * 900))]
        mean = pick(rows, "f1_interzone_spread", "mean", e).filter(pl.col("period_start") == t0)
        assert mean["value"].to_list() == [pytest.approx((20 * 3600 + 1 * 900) / (3600 + 3 * 900))]
        assert pick(rows, "f1_interzone_spread", "n_intervals", e)["value"].to_list() == [4.0]


# --- F2 --------------------------------------------------------------------------------------------


def test_nonpositive_share_counts_zero_and_negative_prices() -> None:
    d = date(2025, 11, 12)
    prices = series("dk.zone.dk1", day_start(d), H, [-5.0, 0.0, 0.01, 10.0, None] + [20.0] * 19)
    rows = zone.f2_nonpositive_price_share(prices, CPH, window_for(d))
    assert pick(rows, "f2_nonpositive_price_share", "daily_share")["value"].to_list() == [pytest.approx(2 / 23)]
    assert pick(rows, "f2_nonpositive_price_share", "n_intervals")["value"].to_list() == [23.0]


def _gen(kind: str, start: datetime, step: timedelta, values: dict[str, list[float | None]]) -> pl.DataFrame:
    return pl.concat(
        [series("dk.zone.dk1", start, step, v, equivalence_class=f"{kind}.{tech}") for tech, v in values.items()]
    )


def test_curtailment_proxy_only_counts_nonpositive_hours() -> None:
    t0 = day_start(date(2025, 10, 1))
    # Four hours of quarter-hourly prices: hour means -1, 5, 0, -3.
    prices = series("dk.zone.dk1", t0, Q, [-10.0, 2.0, 2.0, 2.0] + [5.0] * 4 + [0.0] * 4 + [-3.0] * 4)
    forecast = _gen(
        "generation_forecast",
        t0,
        H,
        {"solar": [10.0, 10.0, 10.0, 10.0], "wind_onshore": [50.0] * 4, "wind_offshore": [40.0] * 4},
    )
    # Actuals at PT15M: summed to hours by the codebook rule.
    actual = _gen(
        "generation_actual",
        t0,
        Q,
        {
            "solar": [2.0] * 16,  # 8 per hour
            "wind_onshore": [10.0] * 16,  # 40 per hour
            "wind_offshore": [5.0] * 8 + [None] + [5.0] * 3 + [30.0] * 4,  # 20, 20, incomplete, 120
        },
    )
    rows = zone.f2_curtailment_proxy_mwh(prices, "mean", forecast, "sum", actual, "sum", CPH, [(t0, t0 + 4 * H)])
    # hour 1: price -1, forecast 100 - actual 68 = 32; hour 2: price 5 -> 0; hour 3: offshore incomplete,
    # not evaluated; hour 4: price -3, forecast 100 - actual 168 < 0 -> 0.
    assert pick(rows, "f2_curtailment_proxy_mwh", "sum").filter(pl.col("period_start") == t0)["value"].to_list() == [
        32.0
    ]
    assert pick(rows, "f2_curtailment_proxy_mwh", "n_intervals")["value"].to_list() == [3.0]


def test_generation_total_needs_every_class() -> None:
    t0 = day_start(date(2025, 10, 1))
    only_wind = _gen("generation_actual", t0, H, {"wind_onshore": [1.0], "wind_offshore": [2.0]})
    assert zone.generation_total(only_wind, "sum", H).is_empty()


# --- F4 --------------------------------------------------------------------------------------------


def test_surplus_index_per_interval() -> None:
    t0 = day_start(date(2026, 3, 29))  # DST start: 23 hours
    gen = _gen(
        "generation_actual",
        t0,
        H,
        {"solar": [0.0] * 23, "wind_onshore": [100.0] * 23, "wind_offshore": [50.0] * 22 + [None]},
    )
    cons = series("dk.zone.dk1", t0, H, [100.0] * 21 + [0.0, 100.0], equivalence_class="consumption_actual")
    rows = zone.f4_surplus_index(gen, "sum", cons, "sum", CPH, window_for(date(2026, 3, 29)))
    # 21 usable hours at (150 - 100) / 100; one hour with zero consumption and one with a null are left out.
    assert pick(rows, "f4_surplus_index", "daily_mean")["value"].to_list() == [pytest.approx(0.5)]
    assert pick(rows, "f4_surplus_index", "share_above_0")["value"].to_list() == [1.0]
    assert pick(rows, "f4_surplus_index", "n_intervals")["value"].to_list() == [21.0]


def test_surplus_index_resamples_to_the_coarsest_input() -> None:
    t0 = day_start(date(2025, 11, 12))
    gen = _gen(
        "generation_actual", t0, Q, {"solar": [1.0] * 4, "wind_onshore": [10.0] * 4, "wind_offshore": [14.0] * 4}
    )
    cons = series("dk.zone.dk1", t0, H, [200.0], equivalence_class="consumption_actual")
    rows = zone.f4_surplus_index(gen, "sum", cons, "sum", CPH, [(t0, t0 + H)])
    assert pick(rows, "f4_surplus_index", "mean")["value"].to_list() == [pytest.approx((100 - 200) / 200)]


# --- F5 --------------------------------------------------------------------------------------------


def test_f5_shares_and_spread_per_regime_sub_window() -> None:
    d1, d2 = date(2025, 3, 18), date(2025, 3, 19)
    brk = day_start(d2)
    direction = pl.concat(
        [
            series("dk.zone.dk1", day_start(d1), Q, [1.0] * 48 + [-1.0] * 24 + [0.0] * 24),
            series("dk.zone.dk1", brk, Q, [-1.0] * 95 + [None]),
        ]
    )
    pers = periods(day_start(d1), local_day_bounds(d2, CPH)[1], [brk])
    rows = zone.f5_activation_shares(direction, CPH, pers)
    up = pick(rows, "f5_activation_share_up", "share")["value"].to_list()
    assert up == [0.5, pytest.approx(48 / 191), 0.0]  # before the break, whole window, after it
    down = pick(rows, "f5_activation_share_down", "share")["value"].to_list()
    assert down == [0.25, pytest.approx(119 / 191), 1.0]
    assert pick(rows, "f5_activation_share_down", "daily_share")["value"].to_list() == [0.25, 1.0]

    imbalance = series("dk.zone.dk1", brk, Q, [-50.0, 100.0, None, 40.0])
    spot = series("dk.zone.dk1", brk, Q, [10.0, 20.0, 30.0, None])
    spread = zone.f5_imbalance_spot_spread(imbalance, spot, CPH, [(brk, brk + H)])
    assert pick(spread, "f5_imbalance_spot_spread", "mean")["value"].to_list() == [70.0]  # (60 + 80) / 2
    assert pick(spread, "f5_imbalance_spot_spread", "n_intervals")["value"].to_list() == [2.0]


def test_f5_rejects_unknown_direction_values() -> None:
    df = series("dk.zone.dk1", day_start(date(2025, 3, 20)), Q, [1.0, 2.0])
    with pytest.raises(ValueError, match="dominating direction"):
        zone.f5_activation_shares(df, CPH, window_for(date(2025, 3, 20)))


# --- registry, sensitivity, provenance ------------------------------------------------------------


def test_feature_inputs_are_accepted_classes(dk_rules: MarketRulesFile) -> None:
    rule_keys = {r.key for r in dk_rules.rules}
    for key, inputs in FEATURE_INPUTS.items():
        spec = FEATURES[key]
        assert spec.outputs and key in RUNNERS
        for role, inp in inputs.items():
            assert inp.equivalence_class in spec.accepted_classes, (key, role)
            assert inp.resolution_rule is None or inp.resolution_rule in rule_keys
        assert set(spec.regime_rules) <= rule_keys


def test_score_inputs_point_at_declared_outputs(root: Path) -> None:
    """The proposed score inputs (assumptions/score_method_v0.yaml) read statistics the features write."""
    sets = load_assumptions(root)
    weights = sets["score_weights_v0"].parameters["weights"].proposed
    method = sets["score_method_v0"].resolve(*sets["score_method_v0"].parameters, allow_proposed=True).values
    inputs = parse_score_method(method).municipality.inputs
    documented = zone.__doc__ or ""
    for si in inputs:
        owner = next(f for f in FEATURES.values() if si.feature_key in f.outputs)
        assert owner.score_weight_key == si.weight_key
        assert si.weight_key in weights
        if si.daily_statistic is None:  # a snapshot feature (F7): no per-day values
            assert re.search(rf"\b{si.statistic}\b", substation.__doc__ or ""), si
            continue
        assert si.daily_statistic.startswith("daily_") and not si.statistic.startswith("daily_")
        line = next(ln for ln in documented.splitlines() if ln.startswith(f"| {si.feature_key} "))
        assert re.search(rf"\b{si.statistic}\b", line.split("|")[3]), line
        assert si.daily_statistic in line.split("|")[2], line


def test_sensitivity_is_the_most_restrictive_input() -> None:
    rows = pl.DataFrame(
        {
            "feature_key": ["f1_intraday_spread_native", "f1_intraday_spread_native", "f1_interzone_spread"],
            "entity_id": ["dk.zone.dk1", "dk.zone.dk2", "dk.zone.dk1"],
            "value": [1.0, 2.0, 3.0],
        }
    )
    inputs = [
        pl.DataFrame({"entity_id": ["dk.zone.dk1", "dk.zone.dk1"], "sensitivity": ["public", "public"]}),
        pl.DataFrame({"entity_id": ["dk.zone.dk2"], "sensitivity": ["licensed"]}),
    ]
    out = attach_sensitivity(rows, inputs).sort("feature_key", "entity_id")
    assert out["sensitivity"].to_list() == ["licensed", "public", "licensed"]


def test_code_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_SHA", "abc1234")
    assert provenance.code_version() == "abc1234"
    monkeypatch.delenv("GIT_SHA")

    def no_git(*args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", no_git)
    assert provenance.code_version() == "unknown"
