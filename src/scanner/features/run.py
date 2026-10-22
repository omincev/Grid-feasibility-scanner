"""Feature runs (WP1.7): load inputs, compute zone features, write feat.feature_value.

`run_features` is the entry point for the Dagster assets (scanner.assets.features) and the CLI. Each
feature group (a FEATURES key) gets its own ops.compute_run row; every row it writes carries that run_id,
the code version and the most restrictive sensitivity of the input series behind it.

Zones (`run_zones`): the run country's zones and reference zones, then every zone of the countries with
status active or reference (registry/countries/<cc>.yaml), each wherever it has data. So every active
country's zones get features from configuration alone (ADR 0011), and the score's percentile population is
every zone available for the feature (docs/features.md, D1 default). Each zone's inputs come from the source
its country's precedence selects and follow its own country's market rules; local days and regime
sub-windows are the run country's for every zone, so daily values pool. When an input is missing or cannot
be used without guessing, the outputs that need it are not written, the reason is logged and stored on the
run (status 'partial', or 'skipped' if nothing was written). The run's params record how many unverified
and verified quarantined input values it loaded (scanner.features.inputs.review_counts). Statistic names:
scanner.features.zone.

F7 (not in the default set; run it by name) reads the current capacity-map facts of the country's
stations instead of zone series; its rules and statistic names are in scanner.features.substation.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import polars as pl
import psycopg

from scanner import REPO_ROOT
from scanner.core.methods import MethodRef
from scanner.core.registry import Country, MarketRulesFile, load_countries, load_market_rules
from scanner.core.sensitivity import Sensitivity, combine
from scanner.features import substation, zone
from scanner.features.inputs import (
    InputError,
    analysis_window,
    empty_obs,
    load_facts,
    load_input,
    periods,
    regime_breaks,
    review_counts,
)
from scanner.features.registry import FEATURE_INPUTS, FEATURES, INTERZONE_PAIRS, FeatureSpec
from scanner.runtime.provenance import ComputeRun, compute_run
from scanner.settings import get_settings

log = logging.getLogger(__name__)

DEFAULT_FEATURES = ("f1_price_spread", "f2_curtailment", "f4_surplus", "f5_balancing_activation")
# Countries whose zones every run computes. Draft country files are not signed off (zone list, codes), so
# their zones join a run only where the run country lists them as reference zones.
RUN_COUNTRY_STATUSES = ("active", "reference")


@dataclass
class FeatureOutcome:
    feature: str
    run_id: str
    status: str
    rows_written: int
    notes: list[str]


@dataclass
class _Run:
    """One feature group's run: window, zones, loaded inputs and what it had to skip (outputs, or
    `<input role>:<country>` where one country's rows of an input were unusable)."""

    conn: psycopg.Connection[Any]
    spec: FeatureSpec
    entity_ids: list[str]
    tz: str
    start: datetime
    end: datetime
    periods: list[tuple[datetime, datetime]]
    # Country code -> market rules (market_rules/<cc>.yaml) and timezone (for the rules' local dates); each
    # zone's inputs follow its own country's rules.
    market_rules: Mapping[str, MarketRulesFile]
    timezones: dict[str, str]
    compute: ComputeRun
    # Country code -> class -> sources in order (registry/countries/<cc>.yaml `precedence`).
    precedence: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    loaded: list[pl.DataFrame] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    country: str = ""

    def load(self, role: str) -> pl.DataFrame:
        """One input, loaded per country so each zone's rows follow its own country's market rules (ADR 0011).
        A country whose rows cannot be used without guessing is left out with a note (the run is then
        partial), so one country's data problem does not cost the other zones their features."""
        spec = FEATURE_INPUTS[self.spec.key][role]
        by_country: dict[str, list[str]] = {}
        for e in self.entity_ids:
            by_country.setdefault(e.split(".")[0], []).append(e)
        frames = [empty_obs()]
        for cc, ids in by_country.items():
            try:
                df = load_input(
                    self.conn,
                    self.spec.accepted_classes,
                    spec,
                    ids,
                    self.start,
                    self.end,
                    rules=self.market_rules.get(cc),
                    tz=self.timezones.get(cc),
                    precedence=self.precedence,
                )
            except InputError as exc:
                self.skipped.append(f"{role}:{cc}")
                self.note(f"input {role} unusable for {', '.join(ids)}: {exc}")
                continue
            frames.append(df)
        df = pl.concat(frames).sort("entity_id", "interval_start")
        self.loaded.append(df)
        return df

    def single(self, df: pl.DataFrame, column: str, role: str) -> Any:
        """The one value of a per-series column (unit, resample rule) across an input, else None."""
        values = df[column].unique().to_list()
        if len(values) != 1:
            self.note(f"input {role} mixes {column} values {values}")
            return None
        return values[0]

    def skip(self, output: str, reason: str) -> None:
        self.skipped.append(output)
        self.note(f"{output} not written: {reason}")

    def note(self, msg: str) -> None:
        log.warning("%s: %s", self.spec.key, msg)
        self.compute.notes.append(msg)


def _f1(r: _Run) -> list[pl.DataFrame]:
    prices = r.load("price")
    if prices.is_empty():
        for output in r.spec.outputs:
            r.skip(output, "no usable day-ahead prices in the window")
        return []
    how = r.single(prices, "resample", "price")
    out = [zone.f1_intraday_spread_native(prices, r.tz, r.periods)]
    if how is None:
        r.skip("f1_intraday_spread_hourly", "no single resampling rule for the price input")
    else:
        out.append(zone.f1_intraday_spread_hourly(prices, how, r.tz, r.periods))
    if r.single(prices, "unit", "price") != "EUR/MWh":
        r.skip("f1_interzone_spread", "the 10 EUR/MWh threshold needs prices in EUR/MWh")
    else:
        out.append(zone.f1_interzone_spread(prices, INTERZONE_PAIRS, r.tz, r.periods))
    return out


def _f2(r: _Run) -> list[pl.DataFrame]:
    out = []
    prices = r.load("price")
    if prices.is_empty():
        r.skip("f2_nonpositive_price_share", "no usable day-ahead prices in the window")
    else:
        out.append(zone.f2_nonpositive_price_share(prices, r.tz, r.periods))

    inputs = {role: r.load(role) for role in FEATURE_INPUTS[r.spec.key] if role != "price"}
    missing = [role for role, df in {"price": prices, **inputs}.items() if df.is_empty()]
    if missing:
        r.skip("f2_curtailment_proxy_mwh", f"no usable data for {missing}")
        return out
    forecast = pl.concat([df for role, df in inputs.items() if role.startswith("forecast_")])
    actual = pl.concat([df for role, df in inputs.items() if role.startswith("actual_")])
    units = {r.single(forecast, "unit", "forecast"), r.single(actual, "unit", "actual")}
    rules = [
        r.single(df, "resample", name) for name, df in (("price", prices), ("forecast", forecast), ("actual", actual))
    ]
    if len(units) != 1 or None in units or None in rules:
        r.skip("f2_curtailment_proxy_mwh", f"forecast and actual units or resampling rules differ: {units}, {rules}")
        return out
    price_how, forecast_how, actual_how = rules
    out.append(
        zone.f2_curtailment_proxy_mwh(prices, price_how, forecast, forecast_how, actual, actual_how, r.tz, r.periods)
    )
    return out


def _f4(r: _Run) -> list[pl.DataFrame]:
    inputs = {role: r.load(role) for role in FEATURE_INPUTS[r.spec.key]}
    missing = [role for role, df in inputs.items() if df.is_empty()]
    if missing:
        r.skip("f4_surplus_index", f"no usable data for {missing}")
        return []
    consumption = inputs.pop("consumption")
    generation = pl.concat(list(inputs.values()))
    units = {r.single(generation, "unit", "generation"), r.single(consumption, "unit", "consumption")}
    gen_how = r.single(generation, "resample", "generation")
    cons_how = r.single(consumption, "resample", "consumption")
    if len(units) != 1 or None in units or gen_how is None or cons_how is None:
        r.skip("f4_surplus_index", f"generation and consumption units or resampling rules differ: {units}")
        return []
    return [zone.f4_surplus_index(generation, gen_how, consumption, cons_how, r.tz, r.periods)]


def _f5(r: _Run) -> list[pl.DataFrame]:
    out = []
    direction = r.load("direction")
    if direction.is_empty():
        for output in ("f5_activation_share_up", "f5_activation_share_down"):
            r.skip(output, "no usable dominating direction in the window")
    else:
        out.append(zone.f5_activation_shares(direction, r.tz, r.periods))
    imbalance, spot = r.load("imbalance_price"), r.load("spot_price")
    if imbalance.is_empty() or spot.is_empty():
        r.skip("f5_imbalance_spot_spread", "no usable imbalance or spot price in the window")
    elif r.single(imbalance, "unit", "imbalance_price") != r.single(spot, "unit", "spot_price"):
        r.skip("f5_imbalance_spot_spread", "imbalance and spot prices have different units")
    else:
        out.append(zone.f5_imbalance_spot_spread(imbalance, spot, r.tz, r.periods))
    return out


def _f7(r: _Run) -> list[pl.DataFrame]:
    """Capacity-map facts of the run country's stations; rows carry their own sensitivity."""
    frames = []
    for role, spec in FEATURE_INPUTS[r.spec.key].items():
        try:
            df = load_facts(r.conn, r.spec.accepted_classes, spec, r.country)
        except InputError as exc:
            r.note(f"input {role} unusable: {exc}")
            continue
        frames.append(df.with_columns(role=pl.lit(role)))
    facts = pl.concat(frames) if frames else pl.DataFrame()
    if facts.is_empty():
        r.skip("f7_substation_saturation", "no capacity-map facts for the country's stations")
        return []
    for role, why in substation.NOT_ASSESSED.items():
        if n := facts.filter((pl.col("role") == role) & pl.col("value").is_not_null()).height:
            r.note(f"input {role}: {n} published values treated as not assessed: {why}")
    return [substation.f7_rows(facts, (r.start, r.end))]


RUNNERS: dict[str, Callable[[_Run], list[pl.DataFrame]]] = {
    "f1_price_spread": _f1,
    "f2_curtailment": _f2,
    "f4_surplus": _f4,
    "f5_balancing_activation": _f5,
    "f7_substation_saturation": _f7,
}
# The method behind each feature group (docs/methods/<key>.md); features take no scenario parameters.
# F7 version 2: TSO consumption not assessed, per-direction counts, MW means, TSO-inclusive and direction-aware
# area statistics.
VERSIONS = {"f7_substation_saturation": 2}
METHODS: dict[str, MethodRef] = {key: MethodRef.declare(key, VERSIONS.get(key, 1)) for key in RUNNERS}


def run_features(
    features: Sequence[str] | None = None,
    *,
    database_url: str | None = None,
    country: str = "dk",
    end_day: date | None = None,
    root: Path = REPO_ROOT,
) -> list[FeatureOutcome]:
    """Compute and write the given feature groups (default: F1, F2, F4, F5) over `run_zones`.

    `country` sets the local days, the window and the regime sub-windows of the run, and the stations F7
    reads. Window: 2025-03-04 00:00 local to the start of `end_day` local (default today, i.e. through the
    latest complete local day). `database_url` defaults to the DATABASE_URL setting.
    """
    keys = list(features or DEFAULT_FEATURES)
    unknown = [k for k in keys if k not in RUNNERS]
    if unknown:
        raise ValueError(f"no runner for features {unknown}; available: {sorted(RUNNERS)}")
    countries = load_countries(root)
    c = countries[country]
    entity_ids = run_zones(countries, country)
    market_rules = load_market_rules(root)
    precedence = {cc: other.precedence for cc, other in countries.items()}
    timezones = {cc: other.timezone for cc, other in countries.items()}
    start, end = analysis_window(c.timezone, end_day)
    with psycopg.connect(database_url or get_settings().database_url) as conn:
        return [
            _run_one(conn, FEATURES[k], entity_ids, start, end, country, market_rules, precedence, timezones)
            for k in keys
        ]


def run_zones(countries: Mapping[str, Country], country: str) -> list[str]:
    """The zones a run computes: the run country's zones and reference zones, then every zone of the
    countries whose status is in RUN_COUNTRY_STATUSES (docs/features.md, "Congestion score": the D1 default
    population is every zone available for the feature). Zones without data are simply absent from the
    outputs."""
    c = countries[country]
    others = [z.id for o in countries.values() if o.status in RUN_COUNTRY_STATUSES for z in o.zones]
    return list(dict.fromkeys([z.id for z in c.zones] + c.reference_zones + others))


def _run_one(
    conn: psycopg.Connection[Any],
    spec: FeatureSpec,
    entity_ids: list[str],
    start: datetime,
    end: datetime,
    country: str,
    market_rules: Mapping[str, MarketRulesFile],
    precedence: dict[str, dict[str, list[str]]],
    timezones: dict[str, str],
) -> FeatureOutcome:
    tz = timezones[country]
    pers = periods(start, end, regime_breaks(market_rules.get(country), spec.regime_rules, tz, start, end))
    params = {
        "country": country,
        "timezone": tz,
        "entities": entity_ids,
        "window": [start.isoformat(), end.isoformat()],
        "sub_windows": [[a.isoformat(), b.isoformat()] for a, b in pers[1:]],
        "regime_rules": list(spec.regime_rules),
        "method": METHODS[spec.key].record(),
    }
    with compute_run(conn, kind="feature", name=spec.key, params=params) as run:
        r = _Run(
            conn, spec, entity_ids, tz, start, end, pers, market_rules, timezones, run, precedence, country=country
        )
        frames = RUNNERS[spec.key](r)
        run.params.update(review_counts(r.loaded))
        rows = pl.concat(frames) if frames else zone.empty_rows()
        if rows.height and "sensitivity" not in rows.columns:  # F7 rows bring their own (fact inputs)
            rows = attach_sensitivity(rows, r.loaded)
        written = write_feature_values(conn, rows, run) if rows.height else 0
        run.status = "skipped" if not written else "partial" if r.skipped else "ok"
        log.info("%s: run %s %s, %d rows", spec.key, run.run_id, run.status, written)
    return FeatureOutcome(spec.key, str(run.run_id), run.status, written, list(run.notes))


def attach_sensitivity(rows: pl.DataFrame, inputs: Sequence[pl.DataFrame]) -> pl.DataFrame:
    """Add each row's sensitivity: the most restrictive class over the input series of its zone (both
    zones of the pair for f1_interzone_spread), per ADR 0008."""
    by_entity: dict[str, list[Sensitivity]] = {}
    for df in inputs:
        for e, s in df.select("entity_id", "sensitivity").unique().iter_rows():
            by_entity.setdefault(e, []).append(s)
    partner = {a: b for a, b in INTERZONE_PAIRS} | {b: a for a, b in INTERZONE_PAIRS}
    keys = rows.select("feature_key", "entity_id").unique()
    classes = []
    for fk, e in keys.iter_rows():
        zones = [e, partner[e]] if fk == "f1_interzone_spread" else [e]
        classes.append(combine(*(s for z in zones for s in by_entity[z])))
    return rows.join(
        keys.with_columns(sensitivity=pl.Series(classes, dtype=pl.String)), on=["feature_key", "entity_id"]
    )


_FEATURE_VALUE_COLUMNS = (
    "feature_key, entity_id, period_start, period_end, statistic, value, sensitivity, run_id, code_version, "
    "method_id, method_version"
)


def write_feature_values(conn: psycopg.Connection[Any], rows: pl.DataFrame, run: ComputeRun) -> int:
    """Rows into feat.feature_value with the run's method (METHODS[run.name]); committed with the run's final
    status. COPY goes to a temporary table first: Postgres refuses COPY FROM into a table with row-level security
    for roles it applies to, such as scanner_analytics (migration 20261018130000)."""
    bad = rows.filter(pl.col("value").is_null() | ~pl.col("value").is_finite())
    if bad.height:
        raise ValueError(f"{bad.height} feature values are null or not finite: {bad.head(3)}")
    method = METHODS[run.name]
    cols = ["feature_key", "entity_id", "period_start", "period_end", "statistic", "value", "sensitivity"]
    conn.execute("create temp table feature_value_stage (like feat.feature_value including defaults) on commit drop")
    with conn.cursor().copy(f"copy feature_value_stage ({_FEATURE_VALUE_COLUMNS}) from stdin") as copy:
        for row in rows.select(cols).iter_rows():
            copy.write_row((*row, run.run_id, run.code_version, method.id, str(method.version)))
    conn.execute(
        f"insert into feat.feature_value ({_FEATURE_VALUE_COLUMNS}) "
        f"select {_FEATURE_VALUE_COLUMNS} from feature_value_stage"
    )
    conn.execute("drop table feature_value_stage")
    return rows.height
