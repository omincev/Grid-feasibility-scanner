"""Offline tests: assumption gating, percentile maths, renormalisation, breakdown and opportunity rules."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scanner import REPO_ROOT, cli
from scanner.core.registry import PROPOSED_LABEL, AssumptionSet, MissingAssumption, load_assumptions, load_scenarios
from scanner.features import substation
from scanner.scores.congestion import Component, MissingInput, percentile, renormalise, score
from scanner.scores.method import NO_STATION_DATA, PARAMETERS, is_area_input, parse_score_method
from scanner.scores.opportunity import evaluate
from scanner.scores.run import F7_KEY
from scanner.valuation.bess import BessParams
from scanner.valuation.run import bess_params

W = (datetime(2025, 3, 3, 23, tzinfo=UTC), datetime(2025, 10, 2, 22, tzinfo=UTC))


def _set(**parameters: dict[str, Any]) -> AssumptionSet:
    return AssumptionSet(
        id="t",
        kind="test",
        status="proposed",
        owners=["owner"],
        approved_by=None,
        approved_on=None,
        sensitivity="public",
        origin="test",
        parameters=parameters,
    )


def test_resolve_falls_back_to_proposed_only_when_allowed() -> None:
    s = _set(a={"value": 1.0}, b={"value": None, "proposed": 2.0}, c={"value": None})
    with pytest.raises(MissingAssumption) as exc:
        s.resolve("a", "b", allow_proposed=False)
    assert (exc.value.names, exc.value.owners) == (["b"], ["owner"])
    r = s.resolve("a", "b", allow_proposed=True)
    assert r.values == {"a": 1.0, "b": 2.0}
    assert r.from_proposed == ("b",) and r.proposed_used
    approved = s.resolve("a", allow_proposed=True)
    assert not approved.proposed_used
    with pytest.raises(MissingAssumption) as exc:  # no value, no proposal: missing even when allowed
        s.resolve("a", "b", "c", "undeclared", allow_proposed=True)
    assert exc.value.names == ["c", "undeclared"]
    assert s.require("a") == {"a": 1.0}
    assert PROPOSED_LABEL == "proposed assumptions (not approved)"


def test_proposed_assumption_files_give_a_valid_battery_and_rules(root: Path) -> None:
    sets = load_assumptions(root)
    bess = sets["bess_mvp"]
    with pytest.raises(MissingAssumption):
        bess.require("charge_efficiency")
    names = ("power_mw", "duration_h", "round_trip_efficiency", "soc_min_fraction", "soc_max_fraction")
    names += ("soc_initial_fraction", "max_cycles_per_day")
    r = bess.resolve(*names, allow_proposed=True)
    assert not r.from_proposed  # approved 2026-10-20
    p = bess_params(r.values)
    assert p.energy_mwh == pytest.approx(2.0) and p.charge_efficiency * p.discharge_efficiency == pytest.approx(0.87)
    for name, param in bess.parameters.items():  # every proposal names its source or says it is a convention
        if param.proposed is not None:
            assert any(s in (param.note or "") for s in ("Danish Energy Agency", "PNNL", "convention")), name
    rules = sets["opportunity_rules_v0"].resolve("high_percentile", "rules", allow_proposed=True)
    assert 0 < rules.values["high_percentile"] < 100
    weights = sets["score_weights_v0"].resolve("weights", allow_proposed=True).values["weights"]
    method = parse_score_method(sets["score_method_v0"].resolve(*PARAMETERS, allow_proposed=True).values)
    assert {si.weight_key for si in method.zone.inputs + method.municipality.inputs} <= set(weights)


def test_bess_params_prefer_split_efficiencies() -> None:
    common = dict(power_mw=1, duration_h=2, soc_min_fraction=0, soc_max_fraction=1, soc_initial_fraction=0)
    split = bess_params({**common, "max_cycles_per_day": 1, "charge_efficiency": 0.98, "discharge_efficiency": 0.97})
    assert (split.charge_efficiency, split.discharge_efficiency, split.energy_mwh) == (0.98, 0.97, 2.0)
    rte = bess_params({**common, "max_cycles_per_day": 1, "round_trip_efficiency": 0.81})
    assert rte == BessParams(1.0, 2.0, 0.9, 0.9, 0.0, 1.0, 0.0, 1.0)


def test_percentile_is_mean_rank() -> None:
    pop = [10.0, 20.0, 30.0, 40.0]
    assert percentile(25.0, pop) == 50.0
    assert percentile(20.0, pop) == pytest.approx(100 * 1.5 / 4)  # ties count half
    assert percentile(0.0, pop) == 0.0 and percentile(50.0, pop) == 100.0
    assert percentile(5.0, [5.0, 5.0, 5.0]) == 50.0
    assert percentile(20.0, pop, higher_is_more=False) == pytest.approx(100 - 37.5)
    assert percentile(0.2, [0.2, 0.6]) == 25.0  # two areas: the degenerate case D1 describes
    with pytest.raises(ValueError):
        percentile(1.0, [])
    with pytest.raises(ValueError):
        percentile(float("nan"), pop)


def test_renormalise_over_present_features() -> None:
    weights = {"f7": 20, "f1": 15, "f2": 15, "f4": 15, "f5": 15, "f3": 10, "f6": 10}
    w = renormalise(weights, ["f1", "f5", "f7"])
    assert w == pytest.approx({"f1": 30.0, "f5": 30.0, "f7": 40.0})
    assert sum(renormalise(weights, ["f1", "f2", "f4", "f5"]).values()) == pytest.approx(100.0)
    with pytest.raises(ValueError):
        renormalise({"a": 0}, ["a"])


def _component(key: str, p: float, raw: float = 1.0) -> Component:
    return Component(key, f"{key}_x", "mean", raw, p, "run-1", W, {"method": "pooled_daily_values"}, "public")


def test_score_breakdown_shape_and_sum() -> None:
    weights = {"f1": 15.0, "f5": 15.0, "f7": 20.0, "f3": 10.0}
    value, features = score([_component("f1", 80.0), _component("f5", 40.0), _component("f7", 25.0)], weights)
    assert value == pytest.approx(0.3 * 80 + 0.3 * 40 + 0.4 * 25)
    assert set(features) == {"f1_x", "f5_x", "f7_x"}
    f1 = features["f1_x"]
    assert {"raw", "percentile", "weight", "contribution"} <= set(f1)
    assert (f1["weight"], f1["set_weight"], f1["weight_key"]) == (pytest.approx(30.0), 15.0, "f1")
    assert sum(f["contribution"] for f in features.values()) == pytest.approx(value)
    assert f1["period"] == [W[0].isoformat(), W[1].isoformat()]
    full, _ = score([_component("f1", 100.0), _component("f5", 100.0)], weights)
    assert full <= 100.0
    with pytest.raises(ValueError):
        score([_component("f1", 1.0), _component("f1", 2.0)], weights)


def test_null_component_keeps_missing_inputs_null_and_does_not_renormalise() -> None:
    weights = {"f1": 15.0, "f5": 15.0, "f7": 20.0, "f3": 10.0}
    comps = [_component("f1", 80.0), _component("f5", 40.0)]
    missing = [MissingInput("f7", "f7_x", "share", NO_STATION_DATA)]
    renorm, renorm_features = score(comps, weights, missing=missing)  # renormalise: f7 is simply absent
    assert renorm == pytest.approx(0.5 * 80 + 0.5 * 40) and "f7_x" not in renorm_features
    value, features = score(comps, weights, policy="null_component", missing=missing)
    assert value == pytest.approx(0.3 * 80 + 0.3 * 40)  # weights over f1, f5 and f7; f7 earns nothing
    f7 = features["f7_x"]
    assert (f7["raw"], f7["percentile"], f7["contribution"], f7["flag"]) == (None, None, None, NO_STATION_DATA)
    assert f7["weight"] == pytest.approx(40.0) and f7["set_weight"] == 20.0
    assert sum(f["weight"] for f in features.values()) == pytest.approx(100.0)
    with pytest.raises(ValueError):
        score(comps, weights, policy="impute")


def _method_values(root: Path, **changes: Any) -> dict[str, Any]:
    s = load_assumptions(root)["score_method_v0"]
    return {**s.resolve(*PARAMETERS, allow_proposed=True).values, **changes}


def test_score_method_parses_the_proposed_set_and_refuses_unknown_choices(root: Path) -> None:
    method = parse_score_method(_method_values(root))
    zone = {si.weight_key: si for si in method.zone.inputs}
    assert zone[F7_KEY].statistic == "share_stations_no_free_production" and zone[F7_KEY].daily_statistic is None
    assert zone[F7_KEY].higher_is_more_opportunity and zone[F7_KEY].is_area_input
    assert zone["f1_price_spread"].daily_statistic == "daily_spread"
    assert (method.zone.missing_input_policy, method.municipality.missing_input_policy) == ("renormalise",) * 2
    assert is_area_input(F7_KEY) and not is_area_input("f1_price_spread")
    for bad in (
        {"municipality_missing_input_policy": "impute"},
        {"zone_inputs": ["f1_price_spread", "f9_unknown"]},
        {"zone_inputs": ["f1_price_spread", "f1_price_spread"]},
        {"day_based_population": "window_values"},
        {"inputs": {"f7_substation_saturation": {**_method_values(root)["inputs"]["f1_price_spread"]}}},
        {"municipality_grid_direction": "both"},
        {"municipality_grid_direction": "load", "municipality_direction_inputs": {"production": {}}},
        {"municipality_direction_inputs": {"sideways": {}}},
        {
            "municipality_direction_inputs": {
                "load": {"f1_price_spread": _method_values(root)["inputs"]["f1_price_spread"]}
            }
        },
        {"asset_grid_directions": {"bess": "storage"}},
        {"municipality_min_stations": 0},
        {"municipality_min_stations": True},
    ):
        with pytest.raises(ValueError):
            parse_score_method(_method_values(root, **bad))


def test_grid_direction_replaces_the_municipality_area_inputs_only(root: Path) -> None:
    """f7_direction_aware and f7_mw_weighted: the site score's F7 statistic follows the asset types' direction."""
    default = parse_score_method(_method_values(root))
    assert default.municipality_grid_direction == "any" and default.municipality_min_stations == 3
    assert default.site_asset_types == ["bess", "dsf", "modular_dc"]
    scenarios = load_scenarios(REPO_ROOT)
    for scenario_id, statistic in (
        ("f7_direction_aware", "share_stations_free_consumption_no_free_production"),
        ("f7_mw_weighted", "free_mw_consumption_mean_no_free_production"),
    ):
        method = parse_score_method(_method_values(root, **scenarios[scenario_id].values_for("score_method")))
        site = {si.weight_key: si for si in method.municipality.inputs}
        assert site[F7_KEY].statistic == statistic and site[F7_KEY].higher_is_more_opportunity
        assert substation.SAMPLE_STATISTIC[statistic] == "stations_assessed_both"
        assert site["f1_price_spread"] == {si.weight_key: si for si in default.municipality.inputs}["f1_price_spread"]
        assert F7_KEY not in {si.weight_key for si in method.zone.inputs}  # zones as in demo_default
        assert method.site_asset_types == ["bess", "dsf", "modular_dc"]  # every asset type is load-type
    production = parse_score_method(_method_values(root, municipality_grid_direction="production"))
    site = {si.weight_key: si for si in production.municipality.inputs}
    assert site[F7_KEY].statistic == "share_stations_free_production_no_free_consumption"
    assert production.site_asset_types == []  # no production-type asset type yet
    assert scenarios["demo_default"].values_for("score_method").keys() == {
        "zone_inputs",
        "municipality_missing_input_policy",
    }


RULES = {
    "bess": {
        "high": ["f1_price_spread", "f5_balancing_activation"],
        "substation_not_saturated": "production_or_consumption",
    },
    "dsf": {"high": ["f5_balancing_activation"], "requires": ["industrial_load_nearby"]},
    "modular_dc": {
        "high": ["f2_curtailment", "f4_surplus"],
        "substation": {"production": "saturated", "consumption": "not_saturated"},
    },
}


def _breakdown(capacity: dict[str, float] | None = None, **percentiles: float) -> dict[str, Any]:
    b: dict[str, Any] = {"features": {f"{k}_x": {"weight_key": k, "percentile": p} for k, p in percentiles.items()}}
    if capacity is not None:
        b["capacity"] = capacity
    return b


def test_opportunity_rules() -> None:
    cap = {"share_stations_no_free_production": 1.0, "share_stations_no_free_consumption": 0.5}
    b = _breakdown(cap, f1_price_spread=80, f5_balancing_activation=75, f2_curtailment=90, f4_surplus=74.9)

    bess = evaluate("bess", RULES["bess"], 75, b)
    assert (bess.status, bess.matched) == ("match", True)
    station = bess.conditions[-1]
    assert station["passed"] is True  # production saturated everywhere, but consumption has room
    assert [c["passed"] for c in station["any_of"]] == [False, True]
    assert bess.conditions[0] == {
        "condition": "high_percentile",
        "feature": "f1_price_spread",
        "value": 80.0,
        "operator": ">=",
        "threshold": 75,
        "passed": True,
        "data": "market",
    }

    dsf = evaluate("dsf", RULES["dsf"], 75, b)
    assert (dsf.status, dsf.matched) == ("candidate_needs_site_data", False)

    dc = evaluate("modular_dc", RULES["modular_dc"], 75, b)
    assert dc.status == "no_match"  # f4 just below the threshold
    assert [(c["condition"], c["passed"]) for c in dc.conditions][1:] == [
        ("high_percentile", False),
        ("substation_saturated", True),
        ("substation_not_saturated", True),
    ]
    assert evaluate("modular_dc", RULES["modular_dc"], 70, b).status == "match"


def test_opportunity_rules_with_missing_data() -> None:
    no_capacity = _breakdown(f1_price_spread=80, f5_balancing_activation=75)
    assert evaluate("bess", RULES["bess"], 75, no_capacity).status == "candidate_needs_site_data"
    assert evaluate("dsf", RULES["dsf"], 80, no_capacity).status == "no_match"
    missing_feature = evaluate("modular_dc", RULES["modular_dc"], 75, no_capacity)
    assert missing_feature.status == "no_match"  # market data missing is not "needs site data"
    assert missing_feature.conditions[0]["value"] is None and missing_feature.conditions[0]["passed"] is None
    saturated = {"share_stations_no_free_production": 1.0, "share_stations_no_free_consumption": 1.0}
    full = _breakdown(saturated, f1_price_spread=80, f5_balancing_activation=75)
    assert evaluate("bess", RULES["bess"], 75, full).status == "no_match"
    with pytest.raises(ValueError):
        evaluate("bess", {"nearby": ["x"]}, 75, full)


def test_cli_commands_registered() -> None:
    parse = cli.build_parser().parse_args
    assert parse(["scores"]).handler is cli.scores_cmd
    a = parse(["valuation", "--end-day", "2025-10-03"])
    assert (a.handler, a.country, a.end_day.isoformat()) == (cli.valuation_cmd, "dk", "2025-10-03")
    assert cli.chosen_scenarios(a) == [None]  # the official scenario as loaded
    two = parse(["scores", "--scenario", "spec_v0", "--scenario", "demo_default"])
    assert cli.chosen_scenarios(two) == ["spec_v0", "demo_default"]
    scenarios = load_scenarios(REPO_ROOT)
    official = next(s.id for s in scenarios.values() if s.status == "official")
    rest = sorted(s for s in scenarios if s != official)
    assert {"demo_default", "f7_direction_aware", "f7_mw_weighted", "spec_v0"} <= set(rest)
    assert cli.chosen_scenarios(parse(["pipeline", "--all-scenarios"])) == [official, *rest]
    with pytest.raises(ValueError):
        cli.chosen_scenarios(parse(["scores", "--all-scenarios", "--scenario", "spec_v0"]))
    assert parse(["report"]).handler is cli.report_cmd
