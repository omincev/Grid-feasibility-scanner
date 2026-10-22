"""Congestion score maths (docs/features.md "Congestion score"): percentiles, weights, breakdown.

Pure functions; scanner.scores.run loads feature values and writes feat.score.

Percentile: the mean rank of a raw value within its reference population, 100 * (below + equal / 2) / n,
so tied values share a percentile and a population of identical values puts everyone at 50. Where a lower
raw value means more opportunity, the percentile is 100 minus that.

Weights, by the level's missing-input policy (scanner.scores.method):
- renormalise: the weights of the inputs present for an entity, scaled to sum to 100;
- null_component: the weights of all the level's inputs, scaled to sum to 100; an input without data stays
  in the breakdown with null raw, percentile and contribution and a flag, and adds nothing to the score.
Score = sum of the known contributions weight * percentile / 100, from 0 to 100.

Breakdown (feat.score.breakdown; serving.zone_feature_breakdown reads raw, percentile, weight and
contribution of every entry under "features"):

    {"features": {<feature_key>: {"raw", "percentile", "weight", "contribution",
                                  "weight_key", "set_weight", "statistic", "run_id", "period",
                                  "population", ["inherited_from"], ["flag"]}},
     ...metadata added by the caller}

<feature_key> and "statistic" name the feat.feature_value rows the raw value comes from (run "run_id",
period "period"); "weight_key" is the key in the weights set and the opportunity rules. Entries of missing
inputs (null_component) have null raw, percentile, contribution, run_id, period and population, and "flag".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np

from scanner.core.sensitivity import Sensitivity


def percentile(value: float, population: Sequence[float], *, higher_is_more: bool = True) -> float:
    """Mean-rank percentile (0 to 100) of value within population; see the module docstring."""
    arr = np.asarray(population, dtype=float)
    if arr.size == 0:
        raise ValueError("empty reference population")
    if not (np.isfinite(arr).all() and np.isfinite(value)):
        raise ValueError("percentile inputs must be finite")
    below = int(np.count_nonzero(arr < value))
    equal = int(np.count_nonzero(arr == value))
    p = 100.0 * (below + 0.5 * equal) / arr.size
    return p if higher_is_more else 100.0 - p


def renormalise(weights: Mapping[str, float], present: Sequence[str]) -> dict[str, float]:
    """The weights of the present keys, scaled to sum to 100."""
    if any(weights[k] < 0 for k in present):
        raise ValueError(f"negative weight among {present}")
    total = sum(weights[k] for k in present)
    if total <= 0:
        raise ValueError(f"weights of {list(present)} sum to {total}; nothing to renormalise")
    return {k: 100.0 * weights[k] / total for k in present}


@dataclass(frozen=True)
class Component:
    """One score input for one entity: raw value, percentile, and what both trace to."""

    weight_key: str
    feature_key: str
    statistic: str
    raw: float
    percentile: float
    run_id: str
    period: tuple[datetime, datetime]
    population: dict[str, Any]  # JSON description of the reference population
    sensitivity: Sensitivity
    inherited_from: str | None = None


@dataclass(frozen=True)
class MissingInput:
    """An input of the entity's level without data for the entity, and why (scanner.scores.method flags)."""

    weight_key: str
    feature_key: str
    statistic: str
    flag: str


def score(
    components: Sequence[Component],
    weights: Mapping[str, float],
    *,
    policy: str = "renormalise",
    missing: Sequence[MissingInput] = (),
) -> tuple[float, dict[str, dict[str, Any]]]:
    """Score value and the breakdown's "features" object for one entity's components (module docstring)."""
    keys = [c.weight_key for c in components]
    if len(set(keys)) != len(keys) or set(keys) & {m.weight_key for m in missing}:
        raise ValueError(f"one component per weight key expected: {keys}, missing {[m.weight_key for m in missing]}")
    if policy == "renormalise":
        used = renormalise(weights, keys)
    elif policy == "null_component":
        used = renormalise(weights, [*keys, *(m.weight_key for m in missing)])
    else:
        raise ValueError(f"unknown missing-input policy {policy!r}")
    features: dict[str, dict[str, Any]] = {}
    for c in components:
        w = used[c.weight_key]
        entry: dict[str, Any] = {
            "raw": c.raw,
            "percentile": c.percentile,
            "weight": w,
            "contribution": w * c.percentile / 100.0,
            "weight_key": c.weight_key,
            "set_weight": float(weights[c.weight_key]),
            "statistic": c.statistic,
            "run_id": c.run_id,
            "period": [c.period[0].isoformat(), c.period[1].isoformat()],
            "population": c.population,
        }
        if c.inherited_from:
            entry["inherited_from"] = c.inherited_from
        features[c.feature_key] = entry
    if policy == "null_component":
        for m in missing:
            features[m.feature_key] = {
                "raw": None,
                "percentile": None,
                "weight": used[m.weight_key],
                "contribution": None,
                "weight_key": m.weight_key,
                "set_weight": float(weights[m.weight_key]),
                "statistic": m.statistic,
                "run_id": None,
                "period": None,
                "population": None,
                "flag": m.flag,
            }
    value = sum(e["contribution"] for e in features.values() if e["contribution"] is not None)
    return min(100.0, max(0.0, value)), features  # clip floating-point noise only
