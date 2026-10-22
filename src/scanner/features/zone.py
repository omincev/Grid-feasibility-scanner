"""Zone features F1, F2, F4 and F5 as pure functions over polars frames (docs/features.md, WP1.7).

Inputs are long frames with at least entity_id, interval_start, interval_end (UTC, half-open) and value,
as returned by scanner.features.inputs.load_input. Explicit nulls are dropped, never filled. Every
function returns rows for feat.feature_value: feature_key, entity_id, period_start, period_end,
statistic, value.

Periods (all UTC, half-open):
- per local day: [local midnight, next local midnight) in the run country's timezone (Europe/Copenhagen
  for dk). Reference zones use the same days, so their daily values pool with the target zones'.
- per window: the analysis window, then each regime sub-window (breaks: the market rules named in the
  feature's FeatureSpec.regime_rules). Window rows cover only intervals inside the period.

Statistic names. Per-local-day rows start with `daily_`; window rows never do.

| feature_key                 | per local day                        | per window                           |
|-----------------------------|--------------------------------------|--------------------------------------|
| f1_intraday_spread_native   | daily_spread                         | mean, p90, n_days                    |
| f1_intraday_spread_hourly   | daily_spread                         | mean, p90, n_days                    |
| f1_interzone_spread         | daily_mean, daily_share_above_10     | mean, share_above_10, n_intervals    |
| f2_nonpositive_price_share  | daily_share                          | share, n_intervals                   |
| f2_curtailment_proxy_mwh    | daily_sum                            | sum, n_intervals                     |
| f4_surplus_index            | daily_mean, daily_share_above_0      | mean, share_above_0, n_intervals     |
| f5_activation_share_up      | daily_share                          | share, n_intervals                   |
| f5_activation_share_down    | daily_share                          | share, n_intervals                   |
| f5_imbalance_spot_spread    | daily_mean                           | mean, n_intervals                    |

Conventions:
- daily_spread is max minus min over one local day, written only for days whose non-null intervals
  cover the whole day (92, 96 or 100 quarter-hours; 23, 24 or 25 hours). mean and p90 (linear
  interpolation) are taken over those days; n_days counts them.
- Means and shares over intervals are weighted by interval duration, so a window spanning the
  2025-10-01 resolution switch is not dominated by the finer resolution. For one resolution this is
  the plain mean or share of intervals. n_intervals counts the intervals evaluated: those where every
  input of the statistic is non-null.
- f1_intraday_spread_hourly: native prices resampled to PT1H with the codebook rule (mean), complete
  hours only. f1_interzone_spread: |DK1 - DK2| per native interval (pairs in INTERZONE_PAIRS), written
  to both zones; share_above_10 counts intervals strictly above 10 EUR/MWh.
- f2_nonpositive_price_share: price <= 0 at native resolution.
- f2_curtailment_proxy_mwh: hourly, the forecast resolution. Per hour, price is the PT1H mean of native
  prices; forecast and actual are wind onshore + wind offshore + solar, each class resampled to PT1H
  with its codebook rule (sum); the hour adds max(0, forecast - actual) if price <= 0, else 0. Hours
  missing any input are not evaluated. A proxy: forecast error has other causes.
- f4_surplus_index: (wind onshore + wind offshore + solar - consumption) / consumption per interval, at
  the coarsest resolution among the inputs (native when they share one). Intervals with consumption
  <= 0 are left out (the ratio is undefined or changes meaning).
- f5: shares over intervals with a non-null dominating direction (values must be -1, 0 or 1);
  f5_imbalance_spot_spread over intervals where both prices are non-null.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

import polars as pl

from scanner.core.time import Resample, resample

log = logging.getLogger(__name__)

UTC_DT = pl.Datetime("us", "UTC")
OUT_SCHEMA: dict[str, Any] = {
    "feature_key": pl.String,
    "entity_id": pl.String,
    "period_start": UTC_DT,
    "period_end": UTC_DT,
    "statistic": pl.String,
    "value": pl.Float64,
}
Period = tuple[datetime, datetime]

INTERZONE_THRESHOLD_EUR_MWH = 10.0  # docs/features.md F1
GENERATION = ("solar", "wind_onshore", "wind_offshore")
DIRECTIONS = (-1.0, 0.0, 1.0)
HOUR = timedelta(hours=1)
_ON = ["entity_id", "interval_start", "interval_end"]


def _length() -> pl.Expr:
    return (pl.col("interval_end") - pl.col("interval_start")).dt.total_seconds()


def with_local_day(df: pl.DataFrame, tz: str) -> pl.DataFrame:
    """Add day_start and day_end: the UTC bounds of the local day containing interval_start."""
    day = pl.col("interval_start").dt.convert_time_zone(tz).dt.date()

    def midnight(d: pl.Expr) -> pl.Expr:
        return d.cast(pl.Datetime("us")).dt.replace_time_zone(tz).dt.convert_time_zone("UTC")

    return df.with_columns(day_start=midnight(day), day_end=midnight(day.dt.offset_by("1d")))


def wmean() -> pl.Expr:
    """Duration-weighted mean of x (weights in column w)."""
    return (pl.col("x") * pl.col("w")).sum() / pl.col("w").sum()


def wshare(cond: pl.Expr) -> pl.Expr:
    """Duration-weighted share of intervals where cond holds."""
    return (pl.col("w") * cond.cast(pl.Float64)).sum() / pl.col("w").sum()


def _long(wide: pl.DataFrame, feature_key: str) -> pl.DataFrame:
    return wide.unpivot(
        index=["entity_id", "period_start", "period_end"], variable_name="statistic", value_name="value"
    ).select(
        pl.lit(feature_key).alias("feature_key"),
        "entity_id",
        "period_start",
        "period_end",
        "statistic",
        pl.col("value").cast(pl.Float64),
    )


def empty_rows() -> pl.DataFrame:
    return pl.DataFrame(schema=OUT_SCHEMA)


def summarise_intervals(
    df: pl.DataFrame, feature_key: str, stats: Mapping[str, pl.Expr], tz: str, periods: Sequence[Period]
) -> pl.DataFrame:
    """Statistics of x over intervals (df: entity_id, interval_start, interval_end, x).

    Per local day: `daily_<name>` for each stat. Per period: `<name>` for each stat, and n_intervals.
    """
    d = with_local_day(df.drop_nulls("x"), tz).with_columns(w=_length().cast(pl.Float64))
    daily = (
        d.group_by("entity_id", "day_start", "day_end")
        .agg(**{f"daily_{name}": e for name, e in stats.items()})
        .rename({"day_start": "period_start", "day_end": "period_end"})
    )
    frames = [_long(daily, feature_key)]
    for p0, p1 in periods:
        sub = d.filter((pl.col("interval_start") >= p0) & (pl.col("interval_end") <= p1))
        agg = sub.group_by("entity_id").agg(**stats, n_intervals=pl.len().cast(pl.Float64))
        agg = agg.with_columns(period_start=pl.lit(p0, dtype=UTC_DT), period_end=pl.lit(p1, dtype=UTC_DT))
        frames.append(_long(agg, feature_key))
    return pl.concat(frames)


def summarise_days(daily: pl.DataFrame, feature_key: str, daily_stat: str, periods: Sequence[Period]) -> pl.DataFrame:
    """Daily values (daily: entity_id, day_start, day_end, x) plus mean, p90 and n_days per period."""
    rows = daily.select("entity_id", period_start="day_start", period_end="day_end", **{daily_stat: "x"})
    frames = [_long(rows, feature_key)]
    for p0, p1 in periods:
        sub = daily.filter((pl.col("day_start") >= p0) & (pl.col("day_end") <= p1))
        agg = sub.group_by("entity_id").agg(
            mean=pl.col("x").mean(),
            p90=pl.col("x").quantile(0.9, interpolation="linear"),
            n_days=pl.len().cast(pl.Float64),
        )
        agg = agg.with_columns(period_start=pl.lit(p0, dtype=UTC_DT), period_end=pl.lit(p1, dtype=UTC_DT))
        frames.append(_long(agg, feature_key))
    return pl.concat(frames)


def to_resolution(
    df: pl.DataFrame, how: Resample, target: timedelta, keys: Sequence[str] = ("entity_id",)
) -> pl.DataFrame:
    """Resample to `target` with the codebook rule (scanner.core.time.resample). Buckets not fully
    covered by non-null source intervals are dropped."""
    d = df.drop_nulls("value").select(*keys, "interval_start", "interval_end", "value", length_s=_length())
    if d.is_empty():
        return d.drop("length_s")
    out = resample(d, target, how)
    complete = pl.col("n") * pl.col("length_s") == int(target.total_seconds())
    return out.filter(complete).select(*keys, "interval_start", "interval_end", "value")


def daily_spread(prices: pl.DataFrame, tz: str) -> pl.DataFrame:
    """Max minus min per entity and local day, for days fully covered by non-null intervals."""
    d = with_local_day(prices.drop_nulls("value"), tz)
    g = d.group_by("entity_id", "day_start", "day_end").agg(
        x=pl.col("value").max() - pl.col("value").min(), covered=_length().sum()
    )
    complete = g.filter(pl.col("covered") == (pl.col("day_end") - pl.col("day_start")).dt.total_seconds())
    if dropped := g.height - complete.height:
        log.info("daily spread: %d zone-days with gaps or nulls left out", dropped)
    return complete.drop("covered")


def f1_intraday_spread_native(prices: pl.DataFrame, tz: str, periods: Sequence[Period]) -> pl.DataFrame:
    return summarise_days(daily_spread(prices, tz), "f1_intraday_spread_native", "daily_spread", periods)


def f1_intraday_spread_hourly(prices: pl.DataFrame, how: Resample, tz: str, periods: Sequence[Period]) -> pl.DataFrame:
    hourly = to_resolution(prices, how, HOUR)
    return summarise_days(daily_spread(hourly, tz), "f1_intraday_spread_hourly", "daily_spread", periods)


def f1_interzone_spread(
    prices: pl.DataFrame, pairs: Sequence[tuple[str, str]], tz: str, periods: Sequence[Period]
) -> pl.DataFrame:
    on = ["interval_start", "interval_end"]
    frames = []
    for a, b in pairs:
        pa = prices.filter(pl.col("entity_id") == a).drop_nulls("value").select(*on, a="value")
        pb = prices.filter(pl.col("entity_id") == b).drop_nulls("value").select(*on, b="value")
        diff = pa.join(pb, on=on, how="inner").select(*on, x=(pl.col("a") - pl.col("b")).abs())
        if not diff.is_empty():
            frames += [diff.with_columns(entity_id=pl.lit(e)) for e in (a, b)]
    if not frames:
        return empty_rows()
    stats = {"mean": wmean(), "share_above_10": wshare(pl.col("x") > INTERZONE_THRESHOLD_EUR_MWH)}
    return summarise_intervals(pl.concat(frames), "f1_interzone_spread", stats, tz, periods)


def f2_nonpositive_price_share(prices: pl.DataFrame, tz: str, periods: Sequence[Period]) -> pl.DataFrame:
    x = prices.select(*_ON, x="value")
    return summarise_intervals(x, "f2_nonpositive_price_share", {"share": wshare(pl.col("x") <= 0)}, tz, periods)


def generation_total(df: pl.DataFrame, how: Resample, target: timedelta) -> pl.DataFrame:
    """Sum over the generation classes per entity and interval at `target`; intervals missing a class
    are dropped. df carries an equivalence_class column with one class per technology in GENERATION."""
    classes = df["equivalence_class"].unique().to_list()
    if len(classes) > len(GENERATION) or not all(c.rsplit(".", 1)[-1] in GENERATION for c in classes):
        raise ValueError(f"unexpected generation classes: {classes}")
    per_class = to_resolution(df, how, target, keys=("entity_id", "equivalence_class"))
    g = per_class.group_by(_ON).agg(value=pl.col("value").sum(), k=pl.col("equivalence_class").n_unique(), n=pl.len())
    return g.filter((pl.col("k") == len(GENERATION)) & (pl.col("n") == len(GENERATION))).select(*_ON, "value")


def f2_curtailment_proxy_mwh(
    prices: pl.DataFrame,
    price_how: Resample,
    forecast: pl.DataFrame,
    forecast_how: Resample,
    actual: pl.DataFrame,
    actual_how: Resample,
    tz: str,
    periods: Sequence[Period],
) -> pl.DataFrame:
    price = to_resolution(prices, price_how, HOUR).rename({"value": "price"})
    fc = generation_total(forecast, forecast_how, HOUR).rename({"value": "forecast"})
    ac = generation_total(actual, actual_how, HOUR).rename({"value": "actual"})
    hours = price.join(fc, on=_ON, how="inner").join(ac, on=_ON, how="inner")
    gap = pl.max_horizontal(pl.col("forecast") - pl.col("actual"), pl.lit(0.0))
    x = pl.when(pl.col("price") <= 0).then(gap).otherwise(pl.lit(0.0))
    return summarise_intervals(
        hours.select(*_ON, x=x), "f2_curtailment_proxy_mwh", {"sum": pl.col("x").sum()}, tz, periods
    )


def f4_surplus_index(
    generation: pl.DataFrame,
    generation_how: Resample,
    consumption: pl.DataFrame,
    consumption_how: Resample,
    tz: str,
    periods: Sequence[Period],
) -> pl.DataFrame:
    lengths = pl.concat([generation.select(s=_length()), consumption.select(s=_length())])["s"]
    target = timedelta(seconds=int(lengths.max()))  # type: ignore[arg-type]
    gen = generation_total(generation, generation_how, target).rename({"value": "generation"})
    cons = to_resolution(consumption, consumption_how, target).rename({"value": "consumption"})
    joined = gen.join(cons, on=_ON, how="inner")
    usable = joined.filter(pl.col("consumption") > 0)
    if dropped := joined.height - usable.height:
        log.warning("f4: %d intervals with consumption <= 0 left out", dropped)
    x = (pl.col("generation") - pl.col("consumption")) / pl.col("consumption")
    stats = {"mean": wmean(), "share_above_0": wshare(pl.col("x") > 0)}
    return summarise_intervals(usable.select(*_ON, x=x), "f4_surplus_index", stats, tz, periods)


def f5_activation_shares(direction: pl.DataFrame, tz: str, periods: Sequence[Period]) -> pl.DataFrame:
    d = direction.drop_nulls("value")
    bad = d.filter(~pl.col("value").is_in(DIRECTIONS))
    if bad.height:
        raise ValueError(f"dominating direction outside {DIRECTIONS}: {bad['value'].unique().to_list()[:5]}")
    x = d.select(*_ON, x="value")
    return pl.concat(
        [
            summarise_intervals(x, "f5_activation_share_up", {"share": wshare(pl.col("x") == 1)}, tz, periods),
            summarise_intervals(x, "f5_activation_share_down", {"share": wshare(pl.col("x") == -1)}, tz, periods),
        ]
    )


def f5_imbalance_spot_spread(
    imbalance: pl.DataFrame, spot: pl.DataFrame, tz: str, periods: Sequence[Period]
) -> pl.DataFrame:
    imb = imbalance.drop_nulls("value").select(*_ON, imb="value")
    sp = spot.drop_nulls("value").select(*_ON, spot="value")
    x = imb.join(sp, on=_ON, how="inner").select(*_ON, x=(pl.col("imb") - pl.col("spot")).abs())
    return summarise_intervals(x, "f5_imbalance_spot_spread", {"mean": wmean()}, tz, periods)
