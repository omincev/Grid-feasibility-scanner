"""F7 substation saturation (docs/features.md, docs/methods/f7_substation_saturation.md) as pure functions over
capacity-map facts.

Input: a long frame with entity_id, zone, municipality, role, value (MW or null), sensitivity, where role
names an input of FEATURE_INPUTS["f7_substation_saturation"] (one current fact per station and role,
scanner.features.inputs.load_facts). The source publishes free MW (rounded by the publisher), so F7
stores free MW: no percentage and no free/limited/none category is derived from it.

Free MW per station, as the source defines the binding value. The capacity map says "Sammenhold
kapaciteten fra Kort 1, 2 og 3 - den laveste værdi angiver den faktiske ledige kapacitet" (the lowest
of the maps is the actual free capacity):
- DSO stations: the lower of the full-access value without the TSO transformer (Kort 1 production,
  Kort 5 consumption) and with it (Kort 2, Kort 6); null if either is null. The transmission value
  (Kort 3, Kort 7) is not applied: it needs a link from the DSO station to its Energinet station,
  which is not an approved match, so the true binding value can be lower than F7's.
- TSO stations: the existing-grid production value (Kort 3). The existing-grid consumption value (Kort 7)
  is not assessed (NOT_ASSESSED): every TSO row publishes 0 MW together with 0 hours at 0 MW, which
  contradict each other, and the source does not say what the 0 means. Inferred reading, pending Energinet's
  answer (docs/questions/capacity-map-semantics.md, question 1): the figures are not published, so F7 gives
  TSO stations no consumption value. The facts stay as published.

Limited-access ranges (capacity.free_mw_limited.*) are not F7 inputs, so no statistic reads a range, an
inverted range or a published upper bound of 0 (the dossier's quirk) as a value.

Rows: feature_key f7_substation_saturation; period = the run's analysis window, like F1 to F5, but the
values are the state known at run time, not an average over the window.

| level                 | statistic                                                                  |
|-----------------------|----------------------------------------------------------------------------|
| substation            | free_mw_consumption, free_mw_production,                                   |
|                       | rank_in_zone_consumption, rank_in_zone_production                          |
| zone and municipality | stations, share_stations_no_free_consumption,                              |
|                       | share_stations_no_free_production, free_mw_consumption_sum,                |
|                       | free_mw_production_sum,                                                    |
|                       | stations_assessed_consumption, stations_assessed_production,               |
|                       | free_mw_consumption_mean, free_mw_production_mean,                         |
|                       | stations_assessed_both,                                                    |
|                       | share_stations_free_consumption_no_free_production,                        |
|                       | share_stations_free_production_no_free_consumption,                        |
|                       | free_mw_consumption_mean_no_free_production,                               |
|                       | free_mw_production_mean_no_free_consumption,                               |
|                       | and every one of these except the two sums with the suffix _incl_tso       |

Conventions:
- A station without a value in a direction has no row for it (null is never 0).
- rank_in_zone_*: ascending free MW among stations with the same zone (the station's parent) and level
  (DSO stations among DSO stations, TSO stations among TSO stations: their MW are not comparable);
  1 = least free capacity; ties share the lowest rank (1, 1, 3). Stations without a zone get no rank.
- Area populations: names without a suffix cover DSO stations only (the BEK 1468 section 29 population);
  names ending in _incl_tso cover DSO and TSO existing-grid stations with a value (TSO stations sit at
  another voltage level and feed the DSO stations below them, so a 150 kV station and its 60/10 kV stations
  both count).
- Per direction d: stations = stations in the area; stations_assessed_d = stations with a value in d;
  share_stations_no_free_d = stations whose free MW is 0 / stations_assessed_d (published 0 means less than
  the publisher's first step: under 10 MW consumption, under 20 MW production); free_mw_d_mean = the sum of
  the stations' free MW / stations_assessed_d. The capacity map publishes no station capacity (transformer
  rating or hosting capacity), so the MW variant cannot be free MW over capacity; it is the mean free MW per
  assessed station. free_mw_d_sum = sum of the stations' values. Sums and means are indicative only: the
  source says station values cannot be added up.
- Direction-aware statistics, over stations assessed in both directions (stations_assessed_both):
  share_stations_free_consumption_no_free_production = stations with consumption above 0 and production 0
  (room for a load at a production-saturated node) / stations_assessed_both; the reverse for
  share_stations_free_production_no_free_consumption; free_mw_consumption_mean_no_free_production = the sum
  of free consumption MW over stations with production 0 / stations_assessed_both (stations with free
  production contribute nothing), and the reverse.
- Counts are written for every area with a station; shares and means only where their denominator is above
  0, sums only where a station has a value. SAMPLE_STATISTIC names the count behind each statistic.
- Sensitivity of a row: the most restrictive class of the facts behind it (ADR 0008).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

import polars as pl

from scanner.core.sensitivity import ORDER

FEATURE_KEY = "f7_substation_saturation"
DIRECTIONS = ("consumption", "production")
# level -> direction -> input roles whose minimum is the station's free MW
ROLES: Mapping[str, Mapping[str, tuple[str, ...]]] = {
    "dso": {
        "consumption": ("dso_consumption_no_tso", "dso_consumption_with_tso"),
        "production": ("dso_production_no_tso", "dso_production_with_tso"),
    },
    "tso": {"consumption": ("tso_consumption",), "production": ("tso_production",)},
}
ALL_ROLES = tuple(r for by_dir in ROLES.values() for roles in by_dir.values() for r in roles)
# Input roles whose published values F7 treats as not assessed (null), and why.
NOT_ASSESSED: Mapping[str, str] = {
    "tso_consumption": (
        "every TSO station publishes 0 MW consumption with 0 hours at 0 MW, which contradict each other; "
        "undocumented, treated as not assessed pending Energinet (docs/questions/capacity-map-semantics.md, "
        "question 1)"
    ),
}

# Area populations: statistic name suffix -> station levels counted.
POPULATIONS: Mapping[str, tuple[str, ...]] = {"": ("dso",), "_incl_tso": ("dso", "tso")}
OTHER = {"consumption": "production", "production": "consumption"}


def _base_samples() -> dict[str, str | None]:
    out: dict[str, str | None] = {"stations": None, "stations_assessed_both": None}
    for d in DIRECTIONS:
        out |= {
            f"stations_assessed_{d}": None,
            f"share_stations_no_free_{d}": f"stations_assessed_{d}",
            f"free_mw_{d}_mean": f"stations_assessed_{d}",
            f"free_mw_{d}_sum": f"stations_assessed_{d}",
            f"share_stations_free_{d}_no_free_{OTHER[d]}": "stations_assessed_both",
            f"free_mw_{d}_mean_no_free_{OTHER[d]}": "stations_assessed_both",
        }
    return out


_BASE_SAMPLES = _base_samples()
# Area statistic -> the count statistic (same population) it is computed over; None for the counts themselves.
SAMPLE_STATISTIC: Mapping[str, str | None] = {
    f"{stat}{suffix}": None if count is None else f"{count}{suffix}"
    for suffix in POPULATIONS
    for stat, count in _BASE_SAMPLES.items()
    if not (suffix and stat.endswith("_sum"))
}
AREA_STATISTICS = tuple(SAMPLE_STATISTIC)

UTC_DT = pl.Datetime("us", "UTC")
OUT_SCHEMA = {
    "feature_key": pl.String,
    "entity_id": pl.String,
    "period_start": UTC_DT,
    "period_end": UTC_DT,
    "statistic": pl.String,
    "value": pl.Float64,
    "sensitivity": pl.String,
}


def _sens_rank() -> pl.Expr:
    return pl.col("sensitivity").replace_strict({s: i for i, s in enumerate(ORDER)}, return_dtype=pl.Int64)


def _sens_name(col: str) -> pl.Expr:
    return pl.col(col).replace_strict(dict(enumerate(ORDER)), return_dtype=pl.String)


def _binding(roles: Sequence[str]) -> pl.Expr:
    known = pl.all_horizontal([pl.col(r).is_not_null() for r in roles])
    return pl.when(known).then(pl.min_horizontal([pl.col(r) for r in roles]))


def station_values(facts: pl.DataFrame) -> pl.DataFrame:
    """One row per station: entity_id, level, zone, municipality, free_mw_<direction>, sens (rank)."""
    unknown = set(facts["role"].unique().to_list()) - set(ALL_ROLES)
    if unknown:
        raise ValueError(f"unknown F7 input roles: {sorted(unknown)}")
    g = (
        facts.with_columns(sens=_sens_rank())
        .group_by("entity_id")
        .agg(
            pl.col("zone").first(),
            pl.col("municipality").first(),
            level=pl.col("role").str.split("_").list.first().unique(),
            sens=pl.col("sens").max(),
            **{r: pl.col("value").filter(pl.col("role") == r).first() for r in ALL_ROLES},
        )
    )
    mixed = g.filter(pl.col("level").list.len() != 1)
    if mixed.height:
        raise ValueError(f"stations with DSO and TSO inputs: {mixed['entity_id'].to_list()[:5]}")
    g = g.with_columns(pl.col("level").list.first(), *(pl.lit(None, pl.Float64).alias(r) for r in NOT_ASSESSED))
    free = {
        f"free_mw_{d}": pl.when(pl.col("level") == "dso")
        .then(_binding(ROLES["dso"][d]))
        .otherwise(_binding(ROLES["tso"][d]))
        for d in DIRECTIONS
    }
    return g.with_columns(**free).select("entity_id", "level", "zone", "municipality", *free, "sens")


def _long(wide: pl.DataFrame, entity_col: str, stats: Sequence[str], period: tuple[datetime, datetime]) -> pl.DataFrame:
    return (
        wide.select(pl.col(entity_col).alias("entity_id"), *stats, "sens")
        .unpivot(index=["entity_id", "sens"], variable_name="statistic", value_name="value")
        .drop_nulls("value")
        .select(
            pl.lit(FEATURE_KEY).alias("feature_key"),
            "entity_id",
            pl.lit(period[0], dtype=UTC_DT).alias("period_start"),
            pl.lit(period[1], dtype=UTC_DT).alias("period_end"),
            "statistic",
            pl.col("value").cast(pl.Float64),
            _sens_name("sens").alias("sensitivity"),
        )
    )


def _ratio(num: pl.Expr, den: str) -> pl.Expr:
    return pl.when(pl.col(den) > 0).then(num.cast(pl.Float64) / pl.col(den))


def area_values(stations: pl.DataFrame, area: str, suffix: str = "") -> pl.DataFrame:
    """One row per area (`area` = zone or municipality column) with the area statistics of one population
    (module docstring); column names carry `suffix`."""
    free = {d: pl.col(f"free_mw_{d}") for d in DIRECTIONS}
    both = free["consumption"].is_not_null() & free["production"].is_not_null()
    agg = (
        stations.filter(pl.col(area).is_not_null())
        .group_by(area)
        .agg(
            stations=pl.len().cast(pl.Float64),
            sens=pl.col("sens").max(),
            stations_assessed_both=both.sum().cast(pl.Float64),
            **{f"stations_assessed_{d}": free[d].is_not_null().sum().cast(pl.Float64) for d in DIRECTIONS},
            **{f"zero_{d}": (free[d] == 0).sum() for d in DIRECTIONS},
            **{f"sum_{d}": free[d].sum() for d in DIRECTIONS},
            **{f"only_{d}": (both & (free[d] > 0) & (free[OTHER[d]] == 0)).sum() for d in DIRECTIONS},
            **{f"mw_only_{d}": free[d].filter(both & (free[OTHER[d]] == 0)).sum() for d in DIRECTIONS},
        )
    )
    stats: dict[str, pl.Expr] = {}
    for d in DIRECTIONS:
        n = f"stations_assessed_{d}"
        stats |= {
            f"share_stations_no_free_{d}": _ratio(pl.col(f"zero_{d}"), n),
            f"free_mw_{d}_mean": _ratio(pl.col(f"sum_{d}"), n),
            f"free_mw_{d}_sum": pl.when(pl.col(n) > 0).then(pl.col(f"sum_{d}").cast(pl.Float64)),
            f"share_stations_free_{d}_no_free_{OTHER[d]}": _ratio(pl.col(f"only_{d}"), "stations_assessed_both"),
            f"free_mw_{d}_mean_no_free_{OTHER[d]}": _ratio(pl.col(f"mw_only_{d}"), "stations_assessed_both"),
        }
    wide = agg.with_columns(**stats)
    names = [s for s in _BASE_SAMPLES if f"{s}{suffix}" in SAMPLE_STATISTIC]
    return wide.select(area, "sens", *(pl.col(s).alias(f"{s}{suffix}") for s in names))


def f7_rows(facts: pl.DataFrame, period: tuple[datetime, datetime]) -> pl.DataFrame:
    """feat.feature_value rows (with sensitivity) for every station and every zone and municipality."""
    st = station_values(facts)
    ranked = st.with_columns(
        **{
            f"rank_in_zone_{d}": pl.when(pl.col("zone").is_not_null())
            .then(pl.col(f"free_mw_{d}").rank(method="min").over("zone", "level"))
            .cast(pl.Float64)
            for d in DIRECTIONS
        }
    )
    station_stats = [*(f"free_mw_{d}" for d in DIRECTIONS), *(f"rank_in_zone_{d}" for d in DIRECTIONS)]
    frames = [_long(ranked, "entity_id", station_stats, period)]
    for suffix, levels in POPULATIONS.items():
        population = st.filter(pl.col("level").is_in(levels))
        for area in ("zone", "municipality"):
            wide = area_values(population, area, suffix)
            frames.append(_long(wide, area, [c for c in wide.columns if c not in (area, "sens")], period))
    return pl.concat(frames).select(list(OUT_SCHEMA))


def empty_rows() -> pl.DataFrame:
    return pl.DataFrame(schema=OUT_SCHEMA)
