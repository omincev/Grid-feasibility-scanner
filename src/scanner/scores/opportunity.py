"""Opportunity type rules (docs/features.md "Opportunity type rules", assumptions/opportunity_rules_v0.yaml).

Pure evaluation of one asset type's rule against one entity's congestion-score breakdown. Each condition
is three-valued: passed true, false, or null when the data to decide it is missing. Status:

- no_match: a condition failed, or a market-data condition (a feature percentile) cannot be decided;
- match: every condition passed;
- candidate_needs_site_data: nothing failed and only site-data conditions are undecided (industrial load
  nearby, which has no source in the MVP; substation capacity where the F7 statistics are missing).

Rule keys (provisional reading, pending H7; docs/features.md):

- high: [<weight key>]: that feature's percentile in the breakdown is at least high_percentile
- requires: [industrial_load_nearby]: needs Layer 2 or F12; always undecided for now
- substation_not_saturated: "<direction>_or_<direction>": in at least one direction the area has a station
  with free capacity, share_stations_no_free_<direction> < 1
- substation: {<direction>: saturated | not_saturated}: saturated means the area has a station without free
  capacity in that direction (share > 0), not_saturated one with free capacity (share < 1)

The shares are F7 area statistics, carried in the breakdown under "capacity".
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

Status = Literal["match", "candidate_needs_site_data", "no_match"]
Condition = dict[str, Any]

DIRECTIONS = ("production", "consumption")
SITE_DATA_SOURCES = {"industrial_load_nearby": "no source yet (needs Layer 2 site data or feature F12)"}


@dataclass(frozen=True)
class Opportunity:
    asset_type: str
    status: Status
    conditions: list[Condition]

    @property
    def matched(self) -> bool:
        return self.status == "match"


def _high(feature: str, threshold: float, breakdown: Mapping[str, Any]) -> Condition:
    entry = next((e for e in breakdown["features"].values() if e.get("weight_key") == feature), None)
    value = None if entry is None or entry["percentile"] is None else float(entry["percentile"])
    note = "feature not in the score breakdown" if entry is None else f"no data ({entry.get('flag')})"
    return {
        "condition": "high_percentile",
        "feature": feature,
        "value": value,
        "operator": ">=",
        "threshold": threshold,
        "passed": None if value is None else value >= threshold,
        "data": "market",
        **({"note": note} if value is None else {}),
    }


def _requires(name: str) -> Condition:
    if name not in SITE_DATA_SOURCES:
        raise ValueError(f"unknown requirement {name!r}")
    return {"condition": "requires", "requirement": name, "value": None, "passed": None, "data": "site"}


def _station(direction: str, state: str, breakdown: Mapping[str, Any]) -> Condition:
    """A direction's saturation state from the area's share of stations without free capacity."""
    if direction not in DIRECTIONS or state not in ("saturated", "not_saturated"):
        raise ValueError(f"unknown substation condition {direction}: {state}")
    statistic = f"share_stations_no_free_{direction}"
    raw = (breakdown.get("capacity") or {}).get(statistic)
    value = None if raw is None else float(raw)
    operator, threshold = (">", 0.0) if state == "saturated" else ("<", 1.0)
    passed = None if value is None else (value > threshold if operator == ">" else value < threshold)
    return {
        "condition": f"substation_{state}",
        "direction": direction,
        "statistic": statistic,
        "value": value,
        "operator": operator,
        "threshold": threshold,
        "passed": passed,
        "data": "site",
    }


def _any_of(conditions: list[Condition]) -> bool | None:
    results = [c["passed"] for c in conditions]
    if any(r is True for r in results):
        return True
    return False if all(r is False for r in results) else None


def conditions(rule: Mapping[str, Any], high_percentile: float, breakdown: Mapping[str, Any]) -> list[Condition]:
    out: list[Condition] = []
    for key, spec in rule.items():
        if key == "high":
            out += [_high(f, high_percentile, breakdown) for f in spec]
        elif key == "requires":
            out += [_requires(r) for r in spec]
        elif key == "substation_not_saturated":
            subs = [_station(d, "not_saturated", breakdown) for d in str(spec).split("_or_")]
            out.append(
                {"condition": "substation_not_saturated_any", "any_of": subs, "passed": _any_of(subs), "data": "site"}
            )
        elif key == "substation":
            out += [_station(d, state, breakdown) for d, state in spec.items()]
        else:
            raise ValueError(f"unknown rule key {key!r}")
    return out


def status(conds: list[Condition]) -> Status:
    passed = [c["passed"] for c in conds]
    if any(p is False for p in passed):
        return "no_match"
    if all(p is True for p in passed):
        return "match"
    undecided = [c for c in conds if c["passed"] is None]
    return "candidate_needs_site_data" if all(c["data"] == "site" for c in undecided) else "no_match"


def evaluate(
    asset_type: str, rule: Mapping[str, Any], high_percentile: float, breakdown: Mapping[str, Any]
) -> Opportunity:
    conds = conditions(rule, high_percentile, breakdown)
    return Opportunity(asset_type, status(conds), conds)
