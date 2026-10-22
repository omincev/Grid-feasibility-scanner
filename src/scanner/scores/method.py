"""The method choices of the congestion score and the municipality site scores, read from the scenario's
`score_method` set (assumptions/score_method_v0.yaml; docs/methods/congestion_score.md and
municipality_site_score.md).

Pure parsing and validation: parse_score_method turns the resolved parameter values into a ScoreMethod and
refuses options the code does not implement, so `make contracts` (scanner.core.contracts.check_scenarios)
catches a mistyped method switch before a run does.

Grid direction of a site score (municipality_site_score.md): asset types are load-type (they draw power where
they create value: modular DC, DSF, BESS charging on surplus) or production-type (they feed in), per
`asset_grid_directions`. A scenario scores its municipalities for one direction (`municipality_grid_direction`):
`any` keeps the area inputs of `inputs`; `load` or `production` replaces them with that direction's entries in
`municipality_direction_inputs`, so the site score is the one for the asset types of that direction.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from scanner.features.registry import FEATURES

# Implemented options of each method switch.
MISSING_INPUT_POLICIES = ("renormalise", "null_component")
DAY_BASED_POPULATIONS = ("pooled_daily_values",)
AREA_POPULATIONS = ("peer_areas",)
MUNICIPALITY_INHERITANCE = ("parent_zone_percentiles",)
GRID_DIRECTIONS = ("load", "production")
MUNICIPALITY_GRID_DIRECTIONS = ("any", *GRID_DIRECTIONS)

# Flags of a missing input, per kind of input: area inputs (F7) come from capacity-map stations.
NO_STATION_DATA = "no_station_data"
NO_FEATURE_DATA = "no_feature_data"
# Flag of a municipality whose area input rests on fewer stations than municipality_min_stations (kept, scored).
LOW_STATION_COUNT = "low_station_count"


@dataclass(frozen=True)
class ScoreInput:
    """One feature summary that feeds a score.

    statistic: the window statistic used as the area's raw value (its row whose period is the analysis window).
    daily_statistic: for day-based inputs, the per-local-day statistic of the same feature key whose values,
        pooled over every zone of the feature run, form the percentile population; None for area inputs.
    higher_is_more_opportunity: True if a higher raw value gives a higher percentile.
    """

    weight_key: str
    feature_key: str
    statistic: str
    daily_statistic: str | None
    higher_is_more_opportunity: bool

    @property
    def is_area_input(self) -> bool:
        return is_area_input(self.weight_key)

    @property
    def missing_flag(self) -> str:
        return NO_STATION_DATA if self.is_area_input else NO_FEATURE_DATA


def is_area_input(weight_key: str) -> bool:
    """Substation-level features reach zones and municipalities as area statistics, not daily values."""
    spec = FEATURES.get(weight_key)
    return spec is not None and spec.level == "substation"


@dataclass(frozen=True)
class LevelMethod:
    inputs: tuple[ScoreInput, ...]
    missing_input_policy: str


@dataclass(frozen=True)
class ScoreMethod:
    zone: LevelMethod
    municipality: LevelMethod  # its area inputs are those of municipality_grid_direction
    day_based_population: str
    area_population: str
    municipality_inheritance: str
    municipality_grid_direction: str
    asset_grid_directions: Mapping[str, str]
    municipality_min_stations: int

    @property
    def site_asset_types(self) -> list[str]:
        """The asset types the municipality site scores are for (every one when the direction is `any`)."""
        d = self.municipality_grid_direction
        return sorted(a for a, ad in self.asset_grid_directions.items() if d in ("any", ad))


ZONE_PARAMETERS = ("inputs", "zone_inputs", "zone_missing_input_policy", "day_based_population", "area_population")
MUNICIPALITY_PARAMETERS = (
    "inputs",
    "municipality_inputs",
    "municipality_missing_input_policy",
    "municipality_inheritance",
    "day_based_population",
    "area_population",
    "municipality_grid_direction",
    "municipality_direction_inputs",
    "asset_grid_directions",
    "municipality_min_stations",
)
PARAMETERS = tuple(dict.fromkeys(ZONE_PARAMETERS + MUNICIPALITY_PARAMETERS))


def _input(weight_key: str, spec: Any) -> ScoreInput:
    if weight_key not in FEATURES:
        raise ValueError(f"score input {weight_key}: not a feature group in scanner.features.registry")
    if not isinstance(spec, Mapping):
        raise ValueError(f"score input {weight_key}: expected a mapping, got {spec!r}")
    expected = {"feature_key", "statistic", "daily_statistic", "higher_is_more_opportunity"}
    if set(spec) != expected:
        raise ValueError(f"score input {weight_key}: keys must be {sorted(expected)}, got {sorted(spec)}")
    si = ScoreInput(
        weight_key,
        str(spec["feature_key"]),
        str(spec["statistic"]),
        None if spec["daily_statistic"] is None else str(spec["daily_statistic"]),
        bool(spec["higher_is_more_opportunity"]),
    )
    if si.is_area_input != (si.daily_statistic is None):
        raise ValueError(f"score input {weight_key}: daily_statistic is null exactly for area (substation) inputs")
    return si


def _choice(values: Mapping[str, Any], name: str, options: tuple[str, ...]) -> str:
    v = values[name]
    if v not in options:
        raise ValueError(f"{name} {v!r} is not implemented; options: {', '.join(options)}")
    return str(v)


def _level(values: Mapping[str, Any], inputs: Mapping[str, ScoreInput], level: str) -> LevelMethod:
    keys = values[f"{level}_inputs"]
    if not isinstance(keys, list) or len(set(keys)) != len(keys):
        raise ValueError(f"{level}_inputs must be a list of distinct weight keys, got {keys!r}")
    if unknown := [k for k in keys if k not in inputs]:
        raise ValueError(f"{level}_inputs names {unknown}, which are not in inputs")
    policy = _choice(values, f"{level}_missing_input_policy", MISSING_INPUT_POLICIES)
    return LevelMethod(tuple(inputs[k] for k in keys), policy)


def _direction_inputs(values: Mapping[str, Any]) -> dict[str, dict[str, ScoreInput]]:
    raw = values["municipality_direction_inputs"]
    if not isinstance(raw, Mapping) or set(raw) - set(GRID_DIRECTIONS):
        raise ValueError(f"municipality_direction_inputs must map {GRID_DIRECTIONS} to inputs, got {raw!r}")
    out: dict[str, dict[str, ScoreInput]] = {}
    for direction, specs in raw.items():
        if not isinstance(specs, Mapping):
            raise ValueError(f"municipality_direction_inputs.{direction}: expected a mapping, got {specs!r}")
        out[direction] = {k: _input(k, v) for k, v in specs.items()}
        if day_based := [k for k, si in out[direction].items() if not si.is_area_input]:
            raise ValueError(f"municipality_direction_inputs.{direction}: {day_based} are not area inputs")
    return out


def _municipality(values: Mapping[str, Any], inputs: Mapping[str, ScoreInput]) -> tuple[LevelMethod, str]:
    """The municipality level with its area inputs replaced by those of its grid direction, and the direction."""
    level = _level(values, inputs, "municipality")
    direction = _choice(values, "municipality_grid_direction", MUNICIPALITY_GRID_DIRECTIONS)
    by_direction = _direction_inputs(values)
    if direction == "any":
        return level, direction
    chosen = by_direction.get(direction, {})
    if missing := [si.weight_key for si in level.inputs if si.is_area_input and si.weight_key not in chosen]:
        raise ValueError(f"municipality_direction_inputs.{direction} has no entry for the area inputs {missing}")
    area = tuple(chosen[si.weight_key] if si.is_area_input else si for si in level.inputs)
    return replace(level, inputs=area), direction


def _asset_directions(values: Mapping[str, Any]) -> dict[str, str]:
    raw = values["asset_grid_directions"]
    if not isinstance(raw, Mapping) or not raw or not set(raw.values()) <= set(GRID_DIRECTIONS):
        raise ValueError(f"asset_grid_directions must map asset types to {GRID_DIRECTIONS}, got {raw!r}")
    return {str(k): str(v) for k, v in raw.items()}


def _min_stations(values: Mapping[str, Any]) -> int:
    v = values["municipality_min_stations"]
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        raise ValueError(f"municipality_min_stations must be a positive integer, got {v!r}")
    return v


def parse_score_method(values: Mapping[str, Any]) -> ScoreMethod:
    """A ScoreMethod from the resolved PARAMETERS; ValueError for anything the code does not implement."""
    raw = values["inputs"]
    if not isinstance(raw, Mapping) or not raw:
        raise ValueError(f"inputs must be a non-empty mapping of weight keys, got {raw!r}")
    inputs = {k: _input(k, v) for k, v in raw.items()}
    municipality, direction = _municipality(values, inputs)
    return ScoreMethod(
        zone=_level(values, inputs, "zone"),
        municipality=municipality,
        day_based_population=_choice(values, "day_based_population", DAY_BASED_POPULATIONS),
        area_population=_choice(values, "area_population", AREA_POPULATIONS),
        municipality_inheritance=_choice(values, "municipality_inheritance", MUNICIPALITY_INHERITANCE),
        municipality_grid_direction=direction,
        asset_grid_directions=_asset_directions(values),
        municipality_min_stations=_min_stations(values),
    )
