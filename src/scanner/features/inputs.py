"""Feature inputs: analysis window, regime sub-windows and observations as polars frames.

Observations come from core.observation_effective (the latest version per key, ADR 0003) joined to
core.series and ref.codebook_entry (docs/features.md, ADR 0012). Rows are used when `usable`: quality 'ok',
or quarantined and not rejected by a person (scanner.runtime.quarantine). Each row keeps its ingest run
and review status ('ok', 'unverified', 'verified'); `review_counts` summarises the quarantined ones for the
outputs to record. One call loads one input (`InputSpec`): one codebook
equivalence class, optionally narrowed to named entries, for a set of entities over a UTC half-open
window. Nothing here fills, interpolates or converts units; anything that would need a guess raises
`InputError`.

Several series per entity are resolved in this order (WP3.3, ADR 0002):
1. Sources: if more than one source has data for an entity, the first source listed in its country
   file's `precedence[<class>]` is used and the others are dropped; no entry means InputError.
2. A `resolution_rule`, where the run's market rules define it, keeps rows at the resolution in force.
3. The remaining series of the entity must cover disjoint time spans and are concatenated (one
   measure whose resolution changed, stored as separate series); an overlap raises InputError.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any

import polars as pl
import psycopg

from scanner.core.registry import MarketRulesFile
from scanner.core.time import local_day, local_day_bounds, parse_duration
from scanner.features.registry import ANALYSIS_START_LOCAL, InputSpec

log = logging.getLogger(__name__)

UTC_DT = pl.Datetime("us", "UTC")

OBS_SCHEMA: dict[str, Any] = {
    "series_id": pl.Int64,
    "entity_id": pl.String,
    "entry_id": pl.String,
    "equivalence_class": pl.String,
    "unit": pl.String,
    "resample": pl.String,
    "sensitivity": pl.String,
    "resolution_s": pl.Int64,
    "interval_start": UTC_DT,
    "interval_end": UTC_DT,
    "issued_at": UTC_DT,
    "value": pl.Float64,
    "run_id": pl.String,  # the ingest run that wrote the row
    "review_status": pl.String,  # ok, unverified or verified (core.observation_effective)
}
_SERIES_COLS = list(OBS_SCHEMA)[:8]
_OBS_COLS = ["series_id", "interval_start", "interval_end", "issued_at", "value", "run_id", "review_status"]


class InputError(RuntimeError):
    """An input cannot be used without guessing: ambiguous entries, mixed units, class not accepted."""


def empty_obs() -> pl.DataFrame:
    return pl.DataFrame(schema=OBS_SCHEMA)


def review_counts(frames: Sequence[pl.DataFrame]) -> dict[str, Any]:
    """Quarantined values among loaded inputs, for compute-run params and output metadata: how many are
    unverified (no decision yet) or verified (accepted), and the ingest runs of the unverified ones, so an
    output can say it includes unverified data and a person can find the runs to review."""
    df = pl.concat([f.select("run_id", "review_status") for f in frames]) if frames else empty_obs()
    unverified = df.filter(pl.col("review_status") == "unverified")
    return {
        "unverified_values": unverified.height,
        "verified_values": df.filter(pl.col("review_status") == "verified").height,
        "unverified_ingest_runs": sorted(unverified["run_id"].unique().to_list()),
    }


def analysis_window(tz: str, end_day: date | None = None) -> tuple[datetime, datetime]:
    """[2025-03-04 00:00 local, start of `end_day` local) in UTC. Default `end_day`: today, so the
    window ends with the latest complete local day."""
    end_day = end_day or local_day(datetime.now(UTC), tz)
    start = local_day_bounds(ANALYSIS_START_LOCAL, tz)[0]
    end = local_day_bounds(end_day, tz)[0]
    if end <= start:
        raise ValueError(f"analysis window is empty: end day {end_day} is not after {ANALYSIS_START_LOCAL}")
    return start, end


def regime_breaks(
    rules: MarketRulesFile | None, keys: Sequence[str], tz: str, start: datetime, end: datetime
) -> list[datetime]:
    """Local midnights (UTC) of the `valid_from` dates of the given rule keys strictly inside the window."""
    if rules is None or not keys:
        return []
    out = {local_day_bounds(r.valid_from, tz)[0] for r in rules.rules if r.key in keys and r.valid_from is not None}
    return sorted(t for t in out if start < t < end)


def periods(start: datetime, end: datetime, breaks: Sequence[datetime]) -> list[tuple[datetime, datetime]]:
    """The whole window first, then each regime sub-window if there is at least one break."""
    subs = list(itertools.pairwise([start, *breaks, end]))
    return [(start, end), *subs] if len(subs) > 1 else [(start, end)]


def load_input(
    conn: psycopg.Connection[Any],
    accepted_classes: Sequence[str],
    spec: InputSpec,
    entity_ids: Sequence[str],
    start: datetime,
    end: datetime,
    *,
    rules: MarketRulesFile | None = None,
    tz: str | None = None,
    precedence: Mapping[str, Mapping[str, Sequence[str]]] | None = None,
) -> pl.DataFrame:
    """Observations of one input for `entity_ids` with `start <= interval_start` and `interval_end <= end`.

    Columns: OBS_SCHEMA. The latest stored version per (series, interval, issue time) is used unless a
    person rejected the ingest run that wrote it. Forecast vintages and resolution regimes are resolved as
    `spec` declares; several sources and series per entity as the module docstring says. `precedence` maps a country
    code to its country file's precedence (class -> sources in order).
    """
    if spec.equivalence_class not in accepted_classes:
        raise InputError(f"class {spec.equivalence_class} is not accepted by this feature ({accepted_classes})")
    entries = list(spec.entries)
    if entries:
        wrong = conn.execute(
            "select entry_id, equivalence_class from ref.codebook_entry "
            "where entry_id = any(%s) and equivalence_class <> %s",
            (entries, spec.equivalence_class),
        ).fetchall()
        if wrong:
            raise InputError(f"entries outside class {spec.equivalence_class}: {wrong}")
    series = conn.execute(
        """
        select s.series_id, s.entity_id, s.entry_id, ce.equivalence_class, s.unit, ce.resample,
               s.sensitivity::text, extract(epoch from s.resolution)::bigint, ce.source
        from core.series s
        join ref.codebook_entry ce on ce.entry_id = s.entry_id
        where ce.equivalence_class = %s
          and s.entity_id = any(%s)
          and (cardinality(%s::text[]) = 0 or s.entry_id = any(%s::text[]))
        """,
        (spec.equivalence_class, list(entity_ids), entries, entries),
    ).fetchall()
    if not series:
        return empty_obs()
    meta = pl.DataFrame(series, schema={**{c: OBS_SCHEMA[c] for c in _SERIES_COLS}, "source": pl.String}, orient="row")
    # Filters on series_id and interval_start reach inside the DISTINCT ON of the view; `usable` is
    # applied to the latest version only, so a rejected revision is never replaced by an older one.
    rows = conn.execute(
        """
        select o.series_id, o.interval_start, o.interval_end, o.issued_at, o.value, o.run_id::text,
               o.review_status
        from core.observation_effective o
        where o.series_id = any(%s)
          and o.interval_start >= %s and o.interval_start < %s
          and o.interval_end <= %s
          and o.usable
        """,
        (meta["series_id"].to_list(), start, end, end),
    ).fetchall()
    obs = pl.DataFrame(rows, schema={c: OBS_SCHEMA[c] for c in _OBS_COLS}, orient="row")
    df = meta.join(obs, on="series_id", how="inner")
    df = select_source(df, spec.equivalence_class, precedence or {}).select(list(OBS_SCHEMA))
    df = select_vintage(df, spec)
    if spec.resolution_rule and rules is not None and tz is not None and has_rule(rules, spec.resolution_rule):
        kept = select_by_resolution_rule(df, rules, spec.resolution_rule, tz)
        if dropped := df.height - kept.height:
            log.info(
                "%s: %d rows outside the resolution set by %s left out",
                spec.equivalence_class,
                dropped,
                spec.resolution_rule,
            )
        df = kept
    check_disjoint_series(df)
    check_unique(df)
    return df.sort("entity_id", "interval_start")


def select_source(
    df: pl.DataFrame, equivalence_class: str, precedence: Mapping[str, Mapping[str, Sequence[str]]]
) -> pl.DataFrame:
    """Per entity, the rows of one source: the only one with data, or the first its country lists in
    `precedence[equivalence_class]` (registry/countries/<cc>.yaml). Never a mix of sources."""
    chosen: list[tuple[str, str]] = []
    for entity, sources in df.group_by("entity_id").agg(pl.col("source").unique().sort()).iter_rows():
        if len(sources) == 1:
            chosen.append((entity, sources[0]))
            continue
        cc = entity.split(".")[0]
        order = precedence.get(cc, {}).get(equivalence_class, [])
        first = next((s for s in order if s in sources), None)
        if first is None:
            raise InputError(
                f"{entity}: {equivalence_class} comes from {sources}; registry/countries/{cc}.yaml has no "
                f"precedence for {equivalence_class} naming one of them"
            )
        chosen.append((entity, first))
    keep = pl.DataFrame(chosen, schema={"entity_id": pl.String, "source": pl.String}, orient="row")
    return df.join(keep, on=["entity_id", "source"], how="semi")


def check_disjoint_series(df: pl.DataFrame) -> None:
    """Several series of one entity (one source by now) must not overlap in time: then they are one
    measure stored per resolution (ADR 0002) and concatenate. Overlap would need a guess."""
    spans = (
        df.group_by("entity_id", "series_id")
        .agg(pl.col("interval_start").min().alias("first"), pl.col("interval_end").max().alias("last"))
        .sort("entity_id", "first")
    )
    for a, b in itertools.pairwise(spans.iter_rows(named=True)):
        if a["entity_id"] == b["entity_id"] and b["first"] < a["last"]:
            raise InputError(
                f"{a['entity_id']}: series {a['series_id']} and {b['series_id']} overlap "
                f"({b['first'].isoformat()} < {a['last'].isoformat()}); name the entries or add a precedence"
            )


def has_rule(rules: MarketRulesFile, key: str) -> bool:
    return any(r.key == key and r.zone is None for r in rules.rules)


FACT_SCHEMA: dict[str, Any] = {
    "entity_id": pl.String,
    "zone": pl.String,
    "municipality": pl.String,
    "entry_id": pl.String,
    "unit": pl.String,
    "sensitivity": pl.String,
    "valid_from": UTC_DT,
    "value": pl.Float64,
}


def load_facts(
    conn: psycopg.Connection[Any],
    accepted_classes: Sequence[str],
    spec: InputSpec,
    country: str,
    as_of: datetime | None = None,
) -> pl.DataFrame:
    """The current numeric fact per (entity, entry) of one input, for the entities of a country.

    Current: among facts with valid_from at or before `as_of` (default: the statement time) and, when
    `as_of` is given, known by then, the latest valid_from, then the latest known_at (ADR 0003). Without
    `as_of` every fact known counts, so clock skew between writer and database cannot hide a fact.
    Entities whose attributes.listed is false (no longer in their source) are left out. The input must
    name its entries (facts of several sources are never mixed). A published null (JSON null) gives a
    null value; a text value raises InputError. Columns: FACT_SCHEMA; zone is the entity's parent when
    that parent is a zone, municipality the entity's attributes.municipality.
    """
    if spec.equivalence_class not in accepted_classes:
        raise InputError(f"class {spec.equivalence_class} is not accepted by this feature ({accepted_classes})")
    entries = list(spec.entries)
    if not entries:
        raise InputError(f"fact input {spec.equivalence_class} must name its codebook entries")
    rows = conn.execute(
        """
        select distinct on (f.entity_id, f.attribute)
               f.entity_id, case when p.kind = 'zone' then p.entity_id end, e.attributes ->> 'municipality',
               f.attribute, f.unit, f.sensitivity::text, f.valid_from, f.value_num, f.value_text,
               f.value_json is not null and f.value_json <> 'null'::jsonb
        from core.fact f
        join ref.codebook_entry ce on ce.entry_id = f.attribute
        join ref.entity e on e.entity_id = f.entity_id
        left join ref.entity p on p.entity_id = e.parent_id
        where ce.equivalence_class = %s
          and (cardinality(%s::text[]) = 0 or f.attribute = any(%s::text[]))
          and e.country_code = %s
          and coalesce((e.attributes ->> 'listed')::boolean, true)
          and (%s::timestamptz is null or f.known_at <= %s::timestamptz)
          and (f.valid_from is null or f.valid_from <= coalesce(%s::timestamptz, statement_timestamp()))
        order by f.entity_id, f.attribute, f.valid_from desc nulls last, f.known_at desc, f.fact_id desc
        """,
        (spec.equivalence_class, entries, entries, country, as_of, as_of, as_of),
    ).fetchall()
    bad = [r[0] for r in rows if r[8] is not None or r[9]]
    if bad:
        raise InputError(f"{spec.equivalence_class}: non-numeric facts for {bad[:5]}")
    df = pl.DataFrame([r[:8] for r in rows], schema=FACT_SCHEMA, orient="row")
    return df


def check_unique(df: pl.DataFrame) -> None:
    dupes = df.group_by("entity_id", "interval_start").len().filter(pl.col("len") > 1)
    if dupes.height:
        raise InputError(f"{dupes.height} intervals have more than one value for the same entity: {dupes.head(3)}")


def select_vintage(df: pl.DataFrame, spec: InputSpec) -> pl.DataFrame:
    """Keep one vintage per (series, interval) for rows with an issue time; actuals pass unchanged."""
    issued = df.filter(pl.col("issued_at").is_not_null())
    if issued.is_empty():
        return df
    if spec.vintage != "latest_issued_before_start":
        raise InputError(f"{spec.equivalence_class} has issue times; the input must declare a vintage rule")
    issued = issued.filter(pl.col("issued_at") <= pl.col("interval_start"))
    issued = issued.filter(pl.col("issued_at") == pl.col("issued_at").max().over("series_id", "interval_start"))
    return pl.concat([df.filter(pl.col("issued_at").is_null()), issued])


def select_by_resolution_rule(df: pl.DataFrame, rules: MarketRulesFile, key: str, tz: str) -> pl.DataFrame:
    """Keep rows whose interval length equals the resolution the market rule sets for that time (ADR 0002).

    Country-wide rules only; rule dates are local days, converted to local midnight in UTC.
    """
    expected = pl.lit(None, dtype=pl.Int64)
    spans = [r for r in rules.rules if r.key == key and r.zone is None]
    if not spans:
        raise InputError(f"market rules for {rules.country} have no country-wide {key}")
    for r in spans:
        cond = pl.lit(True)
        if r.valid_from is not None:
            cond = cond & (pl.col("interval_start") >= local_day_bounds(r.valid_from, tz)[0])
        if r.valid_to is not None:
            cond = cond & (pl.col("interval_start") < local_day_bounds(r.valid_to, tz)[0])
        seconds = int(parse_duration(str(r.value)).total_seconds())
        expected = pl.when(cond).then(pl.lit(seconds, dtype=pl.Int64)).otherwise(expected)
    length = (pl.col("interval_end") - pl.col("interval_start")).dt.total_seconds()
    return df.filter(length == expected)
