"""BESS return range v0 (WP1.9, docs/valuation.md "BESS v0"): day-ahead prices -> val.run, val.result.

Entry point for the Dagster asset (scanner.assets.valuation) and the CLI command `valuation`, one scenario per
call (assumptions/scenarios/; default the official one). One ops.compute_run (kind 'valuation', name
'bess_return_range_v0', scenario_id set); per zone one val.run row and one val.result row (metric
revenue_eur_per_mw_year, low and high; mid stays null). The battery comes from the scenario's
`bess_template` set; method `bess_v0_daily_lp_day_ahead` (docs/methods/). Both rows record the scenario, the
hashes of the scenario and its set, and the method versions (scanner.runtime.scenarios.OutputColumns).

Zones: the run country's zones and its reference zones (registry/countries/<cc>.yaml), wherever they have
day-ahead prices; for dk: DK1, DK2, DE-LU, NO2, SE3 and SE4. Municipalities get no rows of their own:
their site cards show the parent zone's range (docs/features.md, "Return range v0").

Prices: the F1 day-ahead input (scanner.features.registry.DAY_AHEAD_PRICE, one source per zone by the
country files' precedence) at native resolution (PT1H before 2025-10-01, PT15M after) over the analysis
window, split into the run country's local days as the features are. A day is valued only if its
usable, non-null intervals tile the whole local day at one resolution (23, 24 or 25 hours; 92, 96 or
100 quarter-hours); any other day is skipped, never filled, and counted. Usable means quality ok, or
quarantined and not rejected by a person (scanner.runtime.quarantine); val.run.inputs and the compute
run's params record how many unverified and verified values were loaded. Each valued day is one LP
(scanner.valuation.bess.dispatch_day); rolling 365-day sums come from scanner.valuation.bess.annual_range,
which counts a window only if all 365 of its days were valued.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import psycopg
from psycopg.types.json import Jsonb

from scanner import REPO_ROOT
from scanner.core.methods import MethodRef
from scanner.core.registry import MarketRulesFile, load_countries, load_market_rules
from scanner.core.sensitivity import combine
from scanner.core.time import local_day
from scanner.features.inputs import InputError, analysis_window, load_input, review_counts
from scanner.features.registry import DAY_AHEAD_PRICE
from scanner.features.zone import with_local_day
from scanner.runtime.assumptions import UsedAssumptions
from scanner.runtime.provenance import ComputeRun, compute_run
from scanner.runtime.scenarios import COLUMNS, LoadedScenario, load_scenario, output_columns, scenario_provenance
from scanner.settings import get_settings
from scanner.valuation.bess import BessParams, annual_range, dispatch_day

log = logging.getLogger(__name__)

NAME = "bess_return_range_v0"
TEMPLATE_ROLE = "bess_template"
ASSET_TYPE = "bess"
METRIC = "revenue_eur_per_mw_year"
UNIT = "EUR/MW/year"
PRICE_UNIT = "EUR/MWh"

REQUIRED = (
    "power_mw",
    "duration_h",
    "soc_min_fraction",
    "soc_max_fraction",
    "soc_initial_fraction",
    "max_cycles_per_day",
)
SPLIT_EFFICIENCIES = ("charge_efficiency", "discharge_efficiency")
METHOD = MethodRef.declare(
    "bess_v0_daily_lp_day_ahead",
    1,
    tuple(f"{TEMPLATE_ROLE}.{p}" for p in (*REQUIRED, "round_trip_efficiency", *SPLIT_EFFICIENCIES)),
)

DayPrices = tuple[np.ndarray, float]  # prices in interval order, interval length in hours


@dataclass
class ValuationOutcome:
    name: str
    run_id: str
    status: str
    rows_written: int
    notes: list[str] = field(default_factory=list)
    scenario_id: str | None = None


def bess_assumptions(conn: psycopg.Connection[Any], sc: LoadedScenario, *, allow_proposed: bool) -> UsedAssumptions:
    """The scenario's battery template: charge and discharge efficiencies when both are set, else the round
    trip. A MissingAssumption lists every missing parameter at once."""
    split = sc.use(conn, TEMPLATE_ROLE, (), optional=SPLIT_EFFICIENCIES, allow_proposed=allow_proposed)
    both = all(k in split.values for k in SPLIT_EFFICIENCIES)
    efficiency = SPLIT_EFFICIENCIES if both else ("round_trip_efficiency",)
    return sc.use(conn, TEMPLATE_ROLE, (*REQUIRED, *efficiency), allow_proposed=allow_proposed)


def bess_params(values: dict[str, Any]) -> BessParams:
    soc_and_cycles = {
        k: float(values[k])
        for k in ("soc_min_fraction", "soc_max_fraction", "soc_initial_fraction", "max_cycles_per_day")
    }
    power, duration = float(values["power_mw"]), float(values["duration_h"])
    if all(k in values for k in SPLIT_EFFICIENCIES):
        return BessParams(
            power_mw=power,
            energy_mwh=power * duration,
            charge_efficiency=float(values["charge_efficiency"]),
            discharge_efficiency=float(values["discharge_efficiency"]),
            **soc_and_cycles,
        )
    return BessParams.from_round_trip(
        power_mw=power,
        duration_h=duration,
        round_trip_efficiency=float(values["round_trip_efficiency"]),
        **soc_and_cycles,
    )


def window_days(start: datetime, end: datetime, tz: str) -> list[date]:
    """The local days of the half-open window [start, end), whose bounds are local midnights."""
    first, last = local_day(start, tz), local_day(end - timedelta(microseconds=1), tz)
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def complete_days(prices: pl.DataFrame, tz: str) -> dict[date, DayPrices]:
    """One zone's prices per local day, for days whose non-null intervals tile the day at one length."""
    out: dict[date, DayPrices] = {}
    d = with_local_day(prices, tz).sort("interval_start")
    for (day_start, day_end), g in d.group_by(["day_start", "day_end"], maintain_order=True):
        starts, ends = g["interval_start"].to_list(), g["interval_end"].to_list()
        lengths = {e - s for s, e in zip(starts, ends, strict=True)}
        gapless = all(e == s for e, s in zip(ends[:-1], starts[1:], strict=True))
        tiles = starts[0] == day_start and ends[-1] == day_end and gapless
        if g["value"].null_count() == 0 and len(lengths) == 1 and tiles:
            out[local_day(day_start, tz)] = (g["value"].to_numpy(), lengths.pop().total_seconds() / 3600)
    return out


def daily_revenue(days: dict[date, DayPrices], params: BessParams) -> dict[date, float]:
    return {d: dispatch_day(p, dt, params).revenue_eur for d, (p, dt) in days.items()}


def day_ranges(days: Iterable[date]) -> list[list[str]]:
    """Days collapsed into inclusive [first, last] runs of consecutive days, as ISO dates."""
    runs: list[list[date]] = []
    for d in sorted(days):
        if runs and d - runs[-1][1] == timedelta(days=1):
            runs[-1][1] = d
        else:
            runs.append([d, d])
    return [[a.isoformat(), b.isoformat()] for a, b in runs]


def run_bess_v0(
    *,
    scenario: str | None = None,
    database_url: str | None = None,
    country: str = "dk",
    end_day: date | None = None,
    root: Path = REPO_ROOT,
    allow_proposed: bool | None = None,
) -> ValuationOutcome:
    """Value one battery per zone over the analysis window (2025-03-04 local to the start of `end_day`,
    default today) under one scenario (default the official one). allow_proposed defaults to
    Settings.allow_proposed_assumptions. Raises ScenarioNotLoaded or MissingAssumption."""
    allow = get_settings().allow_proposed_assumptions if allow_proposed is None else allow_proposed
    countries = load_countries(root)
    c = countries[country]
    zones = list(dict.fromkeys([z.id for z in c.zones] + c.reference_zones))
    rules = load_market_rules(root).get(country)
    precedence = {cc: other.precedence for cc, other in countries.items()}
    start, end = analysis_window(c.timezone, end_day)
    with psycopg.connect(database_url or get_settings().database_url) as conn:
        sc = load_scenario(conn, scenario)
        params = {
            "country": country,
            "timezone": c.timezone,
            "zones": zones,
            "window": [start.isoformat(), end.isoformat()],
            "scenario_id": sc.id,
            "scenario_hash": sc.content_hash,
            "template_set": sc.set_id(TEMPLATE_ROLE),
            "methods": [METHOD.record()],
            "allow_proposed_assumptions": allow,
        }
        with compute_run(conn, kind="valuation", name=NAME, params=params, scenario_id=sc.id) as run:
            written = _value_zones(conn, sc, run, zones, start, end, c.timezone, rules, precedence, allow)
    return ValuationOutcome(NAME, str(run.run_id), run.status, written, list(run.notes), sc.id)


def _value_zones(
    conn: psycopg.Connection[Any],
    sc: LoadedScenario,
    run: ComputeRun,
    zones: list[str],
    start: datetime,
    end: datetime,
    tz: str,
    rules: MarketRulesFile | None,
    precedence: dict[str, dict[str, list[str]]],
    allow: bool,
) -> int:
    """val.run and val.result rows per zone with usable prices; the number of zones written."""
    used = bess_assumptions(conn, sc, allow_proposed=allow)
    bess = bess_params(used.values)
    cols = output_columns(sc, [used], METHOD)
    days = window_days(start, end, tz)
    written = 0
    used_prices: list[pl.DataFrame] = []
    for zone in zones:
        try:  # per zone, so one zone's unusable input does not hold back the others
            p = load_input(
                conn,
                (DAY_AHEAD_PRICE.equivalence_class,),
                DAY_AHEAD_PRICE,
                [zone],
                start,
                end,
                rules=rules,
                tz=tz,
                precedence=precedence,
            )
        except InputError as exc:
            run.status = "partial"
            run.notes.append(f"{zone}: day-ahead prices unusable: {exc}")
            continue
        units = p["unit"].unique().to_list()
        if p.is_empty() or units != [PRICE_UNIT]:
            run.status = "partial"
            run.notes.append(f"{zone}: no day-ahead prices in {PRICE_UNIT} in the window (units: {units})")
            continue
        revenue = daily_revenue(complete_days(p, tz), bess)
        if not revenue:
            run.status = "partial"
            run.notes.append(f"{zone}: no complete local day of prices in the window")
            continue
        skipped = [d for d in days if d not in revenue]
        rng = annual_range(revenue, bess.power_mw)
        series_keys = [
            k
            for (k,) in conn.execute(
                "select series_key from core.series where series_id = any(%s) order by series_key",
                (p["series_id"].unique().to_list(),),
            )
        ]
        prov = scenario_provenance(sc, used)
        inputs = {
            **prov,
            "method": METHOD.record(),
            "parameters": used.values,
            "battery": asdict(bess),
            "price_series": series_keys,
            "timezone": tz,
            "days_in_window": len(days),
            "days_valued": len(revenue),
            "skipped_days": len(skipped),
            "skipped_day_ranges": day_ranges(skipped),
            "complete_365_day_windows": rng.windows,
            "solver": "scipy.optimize.linprog(method='highs')",
            **review_counts([p]),
        }
        sensitivity = combine(used.sensitivity, *p["sensitivity"].unique().to_list())
        valuation_run_id = uuid.uuid4()
        scenario_id, scenario_hash, hashes, method_id, method_version, method_versions = cols.values()
        conn.execute(
            "insert into val.run (valuation_run_id, entity_id, asset_type, template_set_id, method, "
            "method_version, price_window_start, price_window_end, inputs, labels, run_id, sensitivity, "
            "scenario_id, scenario_hash, assumption_hashes, method_versions) "
            "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                valuation_run_id,
                zone,
                ASSET_TYPE,
                used.set_id,
                method_id,
                method_version,
                start,
                end,
                Jsonb(inputs),
                [*rng.labels, *prov["labels"]],
                run.run_id,
                sensitivity,
                scenario_id,
                scenario_hash,
                hashes,
                method_versions,
            ),
        )
        conn.execute(
            f"insert into val.result (valuation_run_id, metric, low, mid, high, unit, sensitivity, {COLUMNS}) "
            "values (%s, %s, %s, null, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                valuation_run_id,
                METRIC,
                rng.low_eur_per_mw_year,
                rng.high_eur_per_mw_year,
                UNIT,
                sensitivity,
                *cols.values(),
            ),
        )
        written += 1
        used_prices.append(p)
        log.info("%s: %d days valued, %d skipped, %d windows", zone, len(revenue), len(skipped), rng.windows)
    run.params.update(review_counts(used_prices))
    if not written:
        run.status = "skipped"
    return written
