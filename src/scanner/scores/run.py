"""Congestion scores and opportunity types (WP1.8, WP1.12): feat.feature_value -> feat.score, feat.opportunity.

Entry points for the Dagster assets (scanner.assets.scores) and the CLI command `scores`, one scenario per
call (assumptions/scenarios/; default the official one):
- run_congestion_scores: one ops.compute_run (kind 'score', name 'congestion', scenario_id set)
- run_opportunity_types: one run (kind 'score', name 'opportunity_types') over the scenario's latest
  congestion run

Methods (docs/methods/): zones are scored by `congestion_score`, municipalities by `municipality_site_score`,
opportunity types by `opportunity_rules`. Every parameter comes from the scenario: weights from its
`score_weights` set, inputs and method choices from its `score_method` set (scanner.scores.method), rules
from its `opportunity_rules` set. Each row records the scenario, the hashes of the scenario and its sets,
and the method versions (scanner.runtime.scenarios.OutputColumns).

Entities: the zones of every country with status 'active' that have at least one zone input, then each
municipality whose parent zone was scored. Reference zones only widen the percentile populations.

Inputs: per input, the latest feature run (status ok or partial) holding that statistic for the entities is
used, whatever the scenario (features are scenario-independent); an entity's raw value is its row with the
longest period (the analysis window), and the population comes from the same run. Populations
(scanner.scores.congestion for the maths):
- day-based inputs (pooled_daily_values): the per-local-day statistic of every zone in the run, inside the
  window, pooled;
- area inputs (peer_areas, F7): the area statistic of every area of the same kind, i.e. every zone for zone
  scores and every municipality of the country for municipality scores.
Municipalities take their parent zone's percentile of every day-based input (parent_zone_percentiles). Their
area inputs are those of the scenario's municipality_grid_direction (scanner.scores.method); a municipality
whose area statistic rests on fewer stations than municipality_min_stations (the statistic's count,
scanner.features.substation.SAMPLE_STATISTIC, from the same feature run) is flagged low_station_count and
scored as any other. The breakdown records the direction, the asset types it serves (`site`) and the
station counts (`station_sample`).

Quarantined inputs (scanner.runtime.quarantine): each breakdown lists, per feature run behind the score,
how many unverified values that run loaded (`unverified_inputs`, null if it recorded no count) and their
total (`unverified_values`); opportunity reasons carry the total.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from scanner import REPO_ROOT
from scanner.core.methods import MethodRef
from scanner.core.registry import PROPOSED_LABEL, Country, load_countries
from scanner.core.sensitivity import Sensitivity, combine
from scanner.features.substation import SAMPLE_STATISTIC
from scanner.runtime.assumptions import UsedAssumptions
from scanner.runtime.provenance import ComputeRun, compute_run
from scanner.runtime.scenarios import COLUMNS, LoadedScenario, load_scenario, output_columns, scenario_provenance
from scanner.scores.congestion import Component, MissingInput, percentile, score
from scanner.scores.method import (
    LOW_STATION_COUNT,
    MUNICIPALITY_PARAMETERS,
    ZONE_PARAMETERS,
    LevelMethod,
    ScoreInput,
    ScoreMethod,
    parse_score_method,
)
from scanner.scores.opportunity import evaluate
from scanner.settings import get_settings

log = logging.getLogger(__name__)

SCORE_KIND = "congestion"
WEIGHTS_ROLE, METHOD_ROLE, RULES_ROLE = "score_weights", "score_method", "opportunity_rules"
ZONE_METHOD = MethodRef.declare(
    "congestion_score",
    1,
    (f"{WEIGHTS_ROLE}.weights", *(f"{METHOD_ROLE}.{p}" for p in ZONE_PARAMETERS)),
)
SITE_METHOD = MethodRef.declare(
    "municipality_site_score",
    2,
    (f"{WEIGHTS_ROLE}.weights", *(f"{METHOD_ROLE}.{p}" for p in MUNICIPALITY_PARAMETERS)),
)
OPPORTUNITY_METHOD = MethodRef.declare("opportunity_rules", 1, (f"{RULES_ROLE}.high_percentile", f"{RULES_ROLE}.rules"))
PERCENTILE_METHOD = "mean rank: 100 * (count below + count equal / 2) / n"

# The F7 area statistics carried in every breakdown for the opportunity rules' substation conditions.
F7_KEY = "f7_substation_saturation"
CAPACITY_STATISTICS = ("stations", "share_stations_no_free_production", "share_stations_no_free_consumption")

POPULATIONS = {
    "zone": (
        "Day-based features: percentile of the zone's window statistic within the per-local-day values of "
        "every zone with data in the window, pooled (D1 default, docs/features.md). Substation-level "
        "features (F7): percentile among the zones that have the statistic."
    ),
    "municipality": (
        "Day-based features inherited from the parent zone (its percentiles against the pooled daily "
        "values). Substation-level features (F7): percentile among the country's municipalities that have "
        "the statistic. MVP provisional, pending H7."
    ),
}


@dataclass
class ScoreOutcome:
    name: str
    run_id: str
    status: str
    rows_written: int
    notes: list[str] = field(default_factory=list)
    scenario_id: str | None = None


@dataclass(frozen=True)
class FeatureRow:
    entity_id: str
    period_start: datetime
    period_end: datetime
    value: float
    sensitivity: Sensitivity


@dataclass(frozen=True)
class Capacity:
    """F7 area statistics for one entity, carried in the breakdown for the opportunity rules."""

    data: dict[str, Any]
    sensitivity: Sensitivity


@dataclass(frozen=True)
class ScoreRow:
    entity_id: str
    period: tuple[datetime, datetime]
    value: float
    breakdown: dict[str, Any]
    sensitivity: Sensitivity
    method: MethodRef
    method_versions: dict[str, int | None]


@dataclass(frozen=True)
class Context:
    """What every row of one congestion run shares: scenario, sets, weights, method, review counts."""

    scenario: LoadedScenario
    used: tuple[UsedAssumptions, ...]
    weights: dict[str, float]
    method: ScoreMethod
    feature_methods: dict[str, tuple[str, int | None]]  # feature run ID -> (method ID, version)


def latest_feature_run(
    conn: psycopg.Connection[Any], feature_key: str, statistic: str, entity_ids: Sequence[str]
) -> uuid.UUID | None:
    """The most recent ok or partial feature run that wrote `statistic` of `feature_key` for any entity."""
    row = conn.execute(
        """
        select f.run_id
        from feat.feature_value f
        join ops.compute_run r on r.run_id = f.run_id
        where f.feature_key = %s and f.statistic = %s and f.entity_id = any(%s)
          and r.kind = 'feature' and r.status in ('ok', 'partial')
        order by r.started_at desc
        limit 1
        """,
        (feature_key, statistic, list(entity_ids)),
    ).fetchone()
    return None if row is None else row[0]


def window_rows(
    conn: psycopg.Connection[Any],
    run_id: uuid.UUID,
    feature_key: str,
    statistic: str,
    kind: str,
    country: str | None = None,
) -> dict[str, FeatureRow]:
    """Per entity of `kind` (and `country`), its row of the statistic with the longest period."""
    rows = conn.execute(
        """
        select distinct on (f.entity_id) f.entity_id, f.period_start, f.period_end, f.value, f.sensitivity::text
        from feat.feature_value f
        join ref.entity e on e.entity_id = f.entity_id
        where f.run_id = %s and f.feature_key = %s and f.statistic = %s and f.value is not null
          and e.kind = %s and (%s::text is null or e.country_code = %s::text)
        order by f.entity_id, f.period_end - f.period_start desc, f.period_end desc
        """,
        (run_id, feature_key, statistic, kind, country, country),
    ).fetchall()
    return {r[0]: FeatureRow(*r) for r in rows}


def daily_rows(
    conn: psycopg.Connection[Any], run_id: uuid.UUID, feature_key: str, statistic: str, start: datetime, end: datetime
) -> list[FeatureRow]:
    """The per-local-day statistic of every zone in the run, for days inside [start, end)."""
    rows = conn.execute(
        """
        select f.entity_id, f.period_start, f.period_end, f.value, f.sensitivity::text
        from feat.feature_value f
        join ref.entity e on e.entity_id = f.entity_id
        where f.run_id = %s and f.feature_key = %s and f.statistic = %s and f.value is not null
          and e.kind = 'zone' and f.period_start >= %s and f.period_end <= %s
        """,
        (run_id, feature_key, statistic, start, end),
    ).fetchall()
    return [FeatureRow(*r) for r in rows]


def _components(
    si: ScoreInput,
    run_id: uuid.UUID,
    raw: dict[str, FeatureRow],
    targets: Sequence[str],
    population_rows: Sequence[FeatureRow],
    population: dict[str, Any],
) -> dict[str, Component]:
    values = [r.value for r in population_rows]
    pop_sensitivity = combine(*{r.sensitivity for r in population_rows})
    population = {**population, "entities": sorted({r.entity_id for r in population_rows}), "n": len(values)}
    out = {}
    for entity in targets:
        if (r := raw.get(entity)) is None:
            continue
        out[entity] = Component(
            weight_key=si.weight_key,
            feature_key=si.feature_key,
            statistic=si.statistic,
            raw=r.value,
            percentile=percentile(r.value, values, higher_is_more=si.higher_is_more_opportunity),
            run_id=str(run_id),
            period=(r.period_start, r.period_end),
            population=population,
            sensitivity=combine(r.sensitivity, pop_sensitivity),
        )
    return out


def zone_components(conn: psycopg.Connection[Any], si: ScoreInput, zone_ids: Sequence[str]) -> dict[str, Component]:
    """Components of one input for the given zones; empty if no feature run has it."""
    run_id = latest_feature_run(conn, si.feature_key, si.statistic, zone_ids)
    if run_id is None:
        return {}
    raw = window_rows(conn, run_id, si.feature_key, si.statistic, "zone")
    population: dict[str, Any]
    if si.daily_statistic is None:  # an area input (scanner.scores.method.ScoreInput)
        pop = list(raw.values())
        population = {"method": "peer_areas", "level": "zone", "statistic": si.statistic}
    else:
        own = [raw[z] for z in zone_ids if z in raw]
        if not own:
            return {}
        start, end = min(r.period_start for r in own), max(r.period_end for r in own)
        pop = daily_rows(conn, run_id, si.feature_key, si.daily_statistic, start, end)
        population = {
            "method": "pooled_daily_values",
            "level": "zone",
            "statistic": si.daily_statistic,
            "window": [start.isoformat(), end.isoformat()],
        }
    return _components(si, run_id, raw, zone_ids, pop, population) if pop else {}


def municipality_components(
    conn: psycopg.Connection[Any], si: ScoreInput, country: str, municipality_ids: Sequence[str]
) -> dict[str, Component]:
    """An area input (F7) for municipalities, ranked among the country's municipalities."""
    run_id = latest_feature_run(conn, si.feature_key, si.statistic, municipality_ids)
    if run_id is None:
        return {}
    raw = window_rows(conn, run_id, si.feature_key, si.statistic, "municipality", country)
    population = {"method": "peer_areas", "level": "municipality", "statistic": si.statistic}
    return _components(si, run_id, raw, municipality_ids, list(raw.values()), population)


def station_samples(
    conn: psycopg.Connection[Any], si: ScoreInput, found: Mapping[str, Component], country: str
) -> dict[str, float]:
    """Per municipality with a component of the area input, the station count its statistic rests on
    (SAMPLE_STATISTIC), from the component's feature run; empty when the statistic names no count or the run
    did not write it."""
    count = SAMPLE_STATISTIC.get(si.statistic) if si.feature_key == F7_KEY else None
    if count is None or not found:
        return {}
    rows: dict[str, FeatureRow] = {}
    for run_id in {uuid.UUID(c.run_id) for c in found.values()}:
        rows |= window_rows(conn, run_id, F7_KEY, count, "municipality", country)
    return {e: rows[e].value for e in found if e in rows}


def capacity_stats(conn: psycopg.Connection[Any], entity_ids: Sequence[str], kind: str) -> dict[str, Capacity]:
    """F7 area statistics (CAPACITY_STATISTICS) per entity from the latest F7 run that has them."""
    run_id = latest_feature_run(conn, F7_KEY, "share_stations_no_free_production", entity_ids)
    if run_id is None:
        return {}
    wanted = set(entity_ids)
    data: dict[str, dict[str, Any]] = {}
    sens: dict[str, list[Sensitivity]] = {}
    for stat in CAPACITY_STATISTICS:
        for e, r in window_rows(conn, run_id, F7_KEY, stat, kind).items():
            if e in wanted:
                data.setdefault(e, {"feature_key": F7_KEY, "run_id": str(run_id)})[stat] = r.value
                sens.setdefault(e, []).append(r.sensitivity)
    return {e: Capacity(d, combine(*sens[e])) for e, d in data.items()}


def feature_run_review(conn: psycopg.Connection[Any], run_ids: Iterable[str]) -> dict[str, int | None]:
    """Unverified input values each feature run recorded in its params (scanner.features.inputs.review_counts);
    None for a run that recorded no count (one written before the counts existed)."""
    ids = [uuid.UUID(r) for r in set(run_ids)]
    rows = conn.execute(
        "select run_id::text, (params->>'unverified_values')::int from ops.compute_run where run_id = any(%s)", (ids,)
    ).fetchall()
    return {r: n for r, n in rows}


def feature_methods(conn: psycopg.Connection[Any], run_ids: Iterable[str]) -> dict[str, tuple[str, int | None]]:
    """Per feature run, its method ID (the run name) and the version it recorded (None before methods were
    recorded)."""
    ids = [uuid.UUID(r) for r in set(run_ids)]
    rows = conn.execute(
        "select run_id::text, name, (params->'method'->>'version')::int from ops.compute_run where run_id = any(%s)",
        (ids,),
    ).fetchall()
    return {r: (name, version) for r, name, version in rows}


def build_row(
    entity_id: str,
    comps: Sequence[Component],
    ctx: Context,
    level: LevelMethod,
    capacity: Capacity | None,
    *,
    level_name: str,
    method: MethodRef,
    missing: Sequence[MissingInput] = (),
    parent: str | None = None,
    review: Mapping[str, int | None] | None = None,
    flags: Sequence[str] = (),
    extra: Mapping[str, Any] | None = None,
) -> ScoreRow:
    """One score row. `review` maps feature run IDs to the unverified values they loaded; the breakdown
    lists them per run, since every zone's values reach a score through the pooled percentiles. `flags` are
    added to the missing-input flags; `extra` entries are added to the breakdown."""
    value, features = score(comps, ctx.weights, policy=level.missing_input_policy, missing=missing)
    daily = [c.period for c in comps if c.population["method"] == "pooled_daily_values"]
    spans = daily or [c.period for c in comps]
    period = (min(s for s, _ in spans), max(e for _, e in spans))
    present = {c.weight_key for c in comps}
    unverified = {r: (review or {}).get(r) for r in sorted({c.run_id for c in comps})}
    upstream = {ctx.feature_methods[c.run_id][0]: ctx.feature_methods[c.run_id][1] for c in comps}
    breakdown: dict[str, Any] = {
        "features": features,
        "score_kind": SCORE_KIND,
        "level": level_name,
        "method": method.record(),
        "window": [period[0].isoformat(), period[1].isoformat()],
        "population": POPULATIONS[level_name],
        "percentile_method": PERCENTILE_METHOD,
        "missing_input_policy": level.missing_input_policy,
        "missing_feature_policy": level.missing_input_policy,  # the key before scenarios existed
        "missing_features": sorted(k for k in ctx.weights if k not in present),
        "missing_inputs": {m.weight_key: m.flag for m in missing},
        "flags": sorted({m.flag for m in missing} | set(flags)),
        "unverified_inputs": unverified,
        "unverified_values": sum(n for n in unverified.values() if n),
        **(extra or {}),
        **scenario_provenance(ctx.scenario, *ctx.used),
    }
    if parent is not None:
        breakdown["parent_zone"] = parent
    sensitivities = [*(u.sensitivity for u in ctx.used), *(c.sensitivity for c in comps)]
    if capacity is not None:
        breakdown["capacity"] = capacity.data
        sensitivities.append(capacity.sensitivity)
    return ScoreRow(entity_id, period, value, breakdown, combine(*sensitivities), method, upstream)


def _missing(level: LevelMethod, present: set[str]) -> list[MissingInput]:
    return [
        MissingInput(si.weight_key, si.feature_key, si.statistic, si.missing_flag)
        for si in level.inputs
        if si.weight_key not in present
    ]


def country_scores(
    conn: psycopg.Connection[Any],
    c: Country,
    ctx: Context,
    zone: LevelMethod,
    municipality: LevelMethod,
    run: ComputeRun,
) -> list[ScoreRow]:
    """Score rows for one active country's zones and their municipalities. `zone` and `municipality` hold
    the inputs that have a weight."""
    zone_ids = [z.id for z in c.zones]
    inherited = [si for si in municipality.inputs if not si.is_area_input]
    by_input = {si.weight_key: zone_components(conn, si, zone_ids) for si in dict.fromkeys([*zone.inputs, *inherited])}
    per_zone = {z: [by_input[si.weight_key][z] for si in zone.inputs if z in by_input[si.weight_key]] for z in zone_ids}
    scored = {z: comps for z, comps in per_zone.items() if comps}
    if not scored:
        run.notes.append(f"{c.code}: no zone has any score input; nothing scored")
        return []
    for si in zone.inputs:
        if missing := [z for z in scored if z not in by_input[si.weight_key]]:
            run.status = "partial"
            how = "weights renormalised without it" if zone.missing_input_policy == "renormalise" else "left null"
            run.notes.append(f"{si.feature_key}.{si.statistic} missing for {', '.join(missing)}; {how}")

    zone_capacity = capacity_stats(conn, list(scored), "zone")
    found_runs = [x.run_id for comps in by_input.values() for x in comps.values()]
    review = feature_run_review(conn, found_runs)
    ctx = replace(ctx, feature_methods=feature_methods(conn, found_runs))
    rows = [
        build_row(
            z,
            comps,
            ctx,
            zone,
            zone_capacity.get(z),
            level_name="zone",
            method=ZONE_METHOD,
            missing=_missing(zone, {x.weight_key for x in comps}),
            review=review,
        )
        for z, comps in scored.items()
    ]

    munis = conn.execute(
        "select entity_id, parent_id from ref.entity where kind = 'municipality' and parent_id = any(%s) "
        "order by entity_id",
        (list(scored),),
    ).fetchall()
    if not munis:
        return rows
    muni_ids = [m for m, _ in munis]
    area_inputs = [si for si in municipality.inputs if si.is_area_input]
    local = [municipality_components(conn, si, c.code, muni_ids) for si in area_inputs]
    samples = {
        si.weight_key: (si, station_samples(conn, si, found, c.code))
        for si, found in zip(area_inputs, local, strict=True)
    }
    muni_capacity = capacity_stats(conn, muni_ids, "municipality")
    local_runs = [x.run_id for found in local for x in found.values()]
    review |= feature_run_review(conn, local_runs)
    ctx = replace(ctx, feature_methods={**ctx.feature_methods, **feature_methods(conn, local_runs)})
    minimum = ctx.method.municipality_min_stations
    site = {"grid_direction": ctx.method.municipality_grid_direction, "asset_types": ctx.method.site_asset_types}
    for m, parent in munis:
        comps = [
            replace(by_input[si.weight_key][parent], inherited_from=parent)
            for si in inherited
            if parent in by_input[si.weight_key]
        ]
        comps += [found[m] for found in local if m in found]
        if comps:
            present = {x.weight_key for x in comps}
            counts = {key: n.get(m) for key, (_, n) in samples.items() if key in present}
            low = any(n is not None and n < minimum for n in counts.values())
            sample = {
                key: {
                    "statistic": SAMPLE_STATISTIC.get(samples[key][0].statistic),
                    "stations": n,
                    "min_stations": minimum,
                }
                for key, n in counts.items()
            }
            rows.append(
                build_row(
                    m,
                    comps,
                    ctx,
                    municipality,
                    muni_capacity.get(m),
                    level_name="municipality",
                    method=SITE_METHOD,
                    missing=_missing(municipality, present),
                    parent=parent,
                    review=review,
                    flags=[LOW_STATION_COUNT] if low else [],
                    extra={"site": site, "station_sample": sample},
                )
            )
    return rows


def write_scores(conn: psycopg.Connection[Any], rows: Sequence[ScoreRow], run: ComputeRun, ctx: Context) -> int:
    weights_set = ctx.scenario.set_id(WEIGHTS_ROLE)
    values = [
        (
            SCORE_KIND,
            r.entity_id,
            r.period[0],
            r.period[1],
            r.value,
            Jsonb(r.breakdown),
            weights_set,
            run.run_id,
            run.code_version,
            r.sensitivity,
            *output_columns(ctx.scenario, ctx.used, r.method, r.method_versions).values(),
        )
        for r in rows
    ]
    with conn.cursor() as cur:
        cur.executemany(
            "insert into feat.score (score_kind, entity_id, period_start, period_end, value, breakdown, "
            f"weights_set_id, run_id, code_version, sensitivity, {COLUMNS}) "
            "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            values,
        )
    return len(rows)


def _weighted(level: LevelMethod, weights: Mapping[str, float], run: ComputeRun, name: str) -> LevelMethod:
    """The level's inputs that have a weight; the others are noted and left out."""
    for si in level.inputs:
        if si.weight_key not in weights:
            run.notes.append(f"{name}: {si.weight_key} has no weight; left out")
    return replace(level, inputs=tuple(si for si in level.inputs if si.weight_key in weights))


def run_congestion_scores(
    *,
    scenario: str | None = None,
    database_url: str | None = None,
    root: Path = REPO_ROOT,
    allow_proposed: bool | None = None,
) -> ScoreOutcome:
    """Score every active country's zones and their municipalities from the latest feature runs, under one
    scenario (default the official one).

    allow_proposed defaults to Settings.allow_proposed_assumptions. Raises ScenarioNotLoaded, or
    MissingAssumption (owners named) when a set has null values that may not fall back to proposed ones; the
    run is then 'failed'.
    """
    allow = get_settings().allow_proposed_assumptions if allow_proposed is None else allow_proposed
    countries = [c for c in load_countries(root).values() if c.status == "active"]
    with psycopg.connect(database_url or get_settings().database_url) as conn:
        sc = load_scenario(conn, scenario)
        params = {
            "score_kind": SCORE_KIND,
            "countries": [c.code for c in countries],
            "scenario_id": sc.id,
            "scenario_hash": sc.content_hash,
            "weights_set": sc.set_id(WEIGHTS_ROLE),
            "method_set": sc.set_id(METHOD_ROLE),
            "methods": [ZONE_METHOD.record(), SITE_METHOD.record()],
            "allow_proposed_assumptions": allow,
        }
        with compute_run(conn, kind="score", name=SCORE_KIND, params=params, scenario_id=sc.id) as run:
            weights_used, method_used = (
                sc.use(
                    conn,
                    role,
                    dict.fromkeys([*ZONE_METHOD.names(role), *SITE_METHOD.names(role)]),
                    allow_proposed=allow,
                )
                for role in (WEIGHTS_ROLE, METHOD_ROLE)
            )
            method = parse_score_method(method_used.values)
            weights = {k: float(w) for k, w in weights_used.values["weights"].items()}
            zone = _weighted(method.zone, weights, run, "zone")
            municipality = _weighted(method.municipality, weights, run, "municipality")
            run.params["inputs"] = {
                "zone": [asdict(si) for si in zone.inputs],
                "municipality": [asdict(si) for si in municipality.inputs],
            }
            run.params["municipality_grid_direction"] = method.municipality_grid_direction
            run.params["site_asset_types"] = method.site_asset_types
            ctx = Context(sc, (weights_used, method_used), weights, method, {})
            rows = [r for c in countries for r in country_scores(conn, c, ctx, zone, municipality, run)]
            written = write_scores(conn, rows, run, ctx) if rows else 0
            if not written:
                run.status = "skipped"
            log.info("congestion scores (%s): run %s %s, %d rows", sc.id, run.run_id, run.status, written)
    return ScoreOutcome(SCORE_KIND, str(run.run_id), run.status, written, list(run.notes), sc.id)


def latest_score_run(conn: psycopg.Connection[Any], scenario_id: str) -> uuid.UUID | None:
    row = conn.execute(
        "select run_id from ops.compute_run where kind = 'score' and name = %s and scenario_id = %s "
        "and status in ('ok', 'partial') order by started_at desc limit 1",
        (SCORE_KIND, scenario_id),
    ).fetchone()
    return None if row is None else row[0]


def _source_run(conn: psycopg.Connection[Any], sc: LoadedScenario, score_run_id: str | None) -> uuid.UUID | None:
    """The congestion run to type: the given one (it must belong to the scenario) or the scenario's latest."""
    if score_run_id is None:
        return latest_score_run(conn, sc.id)
    source = uuid.UUID(score_run_id)
    row = conn.execute("select scenario_id from ops.compute_run where run_id = %s", (source,)).fetchone()
    if row is None or row[0] != sc.id:
        raise ValueError(f"score run {score_run_id} is not a run of scenario {sc.id}")
    return source


def run_opportunity_types(
    *,
    scenario: str | None = None,
    database_url: str | None = None,
    allow_proposed: bool | None = None,
    score_run_id: str | None = None,
) -> ScoreOutcome:
    """Apply the scenario's opportunity rules to every entity of one of its congestion runs (default: its
    latest ok/partial one)."""
    allow = get_settings().allow_proposed_assumptions if allow_proposed is None else allow_proposed
    with psycopg.connect(database_url or get_settings().database_url) as conn:
        sc = load_scenario(conn, scenario)
        source = _source_run(conn, sc, score_run_id)
        params = {
            "scenario_id": sc.id,
            "scenario_hash": sc.content_hash,
            "rules_set": sc.set_id(RULES_ROLE),
            "score_run_id": str(source) if source else None,
            "methods": [OPPORTUNITY_METHOD.record()],
            "allow_proposed_assumptions": allow,
        }
        with compute_run(conn, kind="score", name="opportunity_types", params=params, scenario_id=sc.id) as run:
            used = sc.use(conn, RULES_ROLE, OPPORTUNITY_METHOD.names(RULES_ROLE), allow_proposed=allow)
            high, rules = float(used.values["high_percentile"]), used.values["rules"]
            scores = []
            if source is not None:
                scores = conn.execute(
                    "select entity_id, breakdown, sensitivity::text, method_versions, assumption_hashes "
                    "from feat.score where run_id = %s and score_kind = %s order by entity_id",
                    (source, SCORE_KIND),
                ).fetchall()
            rows = []
            for entity_id, breakdown, sensitivity, score_methods, score_hashes in scores:
                prov = scenario_provenance(sc, used)
                if breakdown.get("proposed_assumptions"):  # the percentiles rest on proposed values too
                    prov |= {"proposed_assumptions": True, "labels": [PROPOSED_LABEL]}
                cols = output_columns(sc, [used], OPPORTUNITY_METHOD, score_methods or {})
                cols = replace(cols, assumption_hashes={**(score_hashes or {}), **cols.assumption_hashes})
                for asset_type, rule in rules.items():
                    opp = evaluate(asset_type, rule, high, breakdown)
                    reasons = {
                        "conditions": opp.conditions,
                        "score_run_id": str(source),
                        "score_assumption_sets": breakdown.get("assumption_sets", []),
                        "unverified_values": breakdown.get("unverified_values"),
                        **prov,
                    }
                    sens = combine(sensitivity, used.sensitivity)
                    opp_row = (entity_id, asset_type, opp.matched, opp.status, Jsonb(reasons), used.set_id, sens)
                    rows.append((*opp_row, *cols.values()))
            with conn.cursor() as cur:
                cur.executemany(
                    "insert into feat.opportunity (entity_id, asset_type, matched, status, reasons, rules_set_id, "
                    f"sensitivity, {COLUMNS}, run_id) "
                    "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    [(*r, run.run_id) for r in rows],
                )
            if not rows:
                run.status = "skipped"
                run.notes.append(f"no congestion scores in run {source}" if source else "no congestion score run yet")
            log.info("opportunity types (%s): run %s %s, %d rows", sc.id, run.run_id, run.status, len(rows))
    return ScoreOutcome("opportunity_types", str(run.run_id), run.status, len(rows), list(run.notes), sc.id)
