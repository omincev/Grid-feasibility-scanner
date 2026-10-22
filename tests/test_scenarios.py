"""Scenarios, method cards, their contract checks and the methods and scenarios report (offline).

DB behaviour (scenario columns, one run per scenario in the serving views, the comparison) is tested in
tests/test_scores_db.py and tests/test_serving.py.
"""

from __future__ import annotations

import shutil
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from scanner import REPO_ROOT
from scanner.core.contracts import check_methods, check_scenarios, referenced_methods
from scanner.core.methods import MethodRef, load_method_cards, parse_card
from scanner.core.registry import Parameter, Scenario, load_assumptions, load_scenarios
from scanner.runtime.report import ReportData, render_html, render_markdown

SPEC: dict[str, Any] = {
    "id": "t",
    "title": "T",
    "status": "exploratory",
    "description": "d",
    "owners": ["oleg"],
    "sets": {"score_method": "score_method_v0"},
}


def test_repository_scenarios_and_cards() -> None:
    scenarios = load_scenarios(REPO_ROOT)
    assert {"spec_v0", "demo_default"} <= set(scenarios)
    assert len([s for s in scenarios.values() if s.status == "official"]) == 1  # the owners' choice
    spec, demo = scenarios["spec_v0"], scenarios["demo_default"]
    assert spec.sets == demo.sets and not spec.method_switches and not spec.overrides
    assert demo.values_for("score_method") == {
        "zone_inputs": ["f1_price_spread", "f2_curtailment", "f4_surplus", "f5_balancing_activation"],
        "municipality_missing_input_policy": "null_component",
    }
    cards = load_method_cards(REPO_ROOT)
    assert set(referenced_methods()) <= set(cards)
    bess = cards["bess_v0_daily_lp_day_ahead"]
    assert "perfect foresight (overstates)" in bess.known_biases and bess.body.strip()
    assert not set(load_assumptions(REPO_ROOT)) & set(scenarios)  # scenario files are not assumption sets


def test_scenario_model_validates_references_and_approval() -> None:
    assert Scenario(**SPEC, method_switches={"score_method.zone_inputs": []}).values_for("score_method")
    for bad in (
        {"method_switches": {"zone_inputs": []}},  # not <role>.<parameter>
        {"overrides": {"opportunity_rules.high_percentile": 70}},  # role without a set
        {"method_switches": {"score_method.x": 1}, "overrides": {"score_method.x": 2}},
        {"approved_by": "oleg"},  # without approved_on
        {"status": "baseline"},
    ):
        with pytest.raises(ValidationError):
            Scenario(**{**SPEC, **bad})


def test_scenario_values_are_proposed_unless_the_scenario_is_approved() -> None:
    params = {"a": Parameter(value=1), "b": Parameter(proposed=2)}
    s = Scenario(**SPEC, overrides={"score_method.a": 10})
    merged, replaced = s.parameters("score_method", params)
    assert replaced == ("a",) and merged["a"].value is None and merged["a"].proposed == 10
    assert merged["b"] == params["b"]
    approved = Scenario(**SPEC, overrides={"score_method.a": 10}, approved_by="oleg", approved_on=date(2026, 10, 19))
    assert approved.parameters("score_method", params)[0]["a"].value == 10
    with pytest.raises(ValueError, match="no parameters"):
        Scenario(**SPEC, overrides={"score_method.c": 1}).parameters("score_method", params)


def test_card_front_matter_is_required_and_checked() -> None:
    text = (REPO_ROOT / "docs" / "methods" / "opportunity_rules.md").read_text(encoding="utf-8")
    card = parse_card(text)
    assert card.id == "opportunity_rules" and card.version == 1 and card.body.startswith("Specification")
    with pytest.raises(ValueError, match="front matter"):
        parse_card("# no front matter\n")
    with pytest.raises(ValidationError):
        parse_card(text.replace("parameters: [opportunity_rules.high_percentile", "parameters: [high_percentile"))


def _root(tmp_path: Path) -> Path:
    for d in ("assumptions", "docs/methods"):
        shutil.copytree(REPO_ROOT / d, tmp_path / d)
    return tmp_path


def _edit(path: Path, **changes: Any) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data.update(changes)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_contract_checks_pass_on_the_repository(tmp_path: Path) -> None:
    root = _root(tmp_path)
    assert check_scenarios(root) == [] and check_methods(root) == []


def test_contract_check_scenarios_fails(tmp_path: Path) -> None:
    root = _root(tmp_path)
    scenarios = root / "assumptions" / "scenarios"
    _edit(scenarios / "spec_v0.yaml", status="official")
    _edit(
        scenarios / "demo_default.yaml",
        method_switches={"score_method.municipality_missing_input_policy": "impute", "score_method.nope": 1},
        sets={**yaml.safe_load((scenarios / "demo_default.yaml").read_text(encoding="utf-8"))["sets"], "x": "gone"},
    )
    errors = check_scenarios(root)
    assert any("exactly one scenario must be official" in e for e in errors), errors
    assert any("set gone (role x) does not exist" in e for e in errors), errors
    assert any("score_method.nope does not resolve" in e for e in errors), errors
    assert any("'impute' is not implemented" in e for e in errors), errors


def test_contract_check_methods_fails(tmp_path: Path) -> None:
    root = _root(tmp_path)
    referenced = dict(referenced_methods())
    referenced["new_method"] = MethodRef("new_method", 1)  # not declared: stays out of the registry
    referenced["opportunity_rules"] = MethodRef("opportunity_rules", 2, ("opportunity_rules.rules",))
    card = root / "docs" / "methods" / "congestion_score.md"
    card.write_text(
        card.read_text(encoding="utf-8").replace("  - score_method.zone_inputs\n", "  - score_method.zone_inputz\n"),
        encoding="utf-8",
    )
    errors = check_methods(root, referenced)
    assert any("new_method is referenced in code but has no card" in e for e in errors), errors
    assert any("opportunity_rules: code implements version 2" in e for e in errors), errors
    assert any("opportunity_rules: card parameters" in e for e in errors), errors
    assert any("congestion_score: parameter score_method.zone_inputz does not resolve" in e for e in errors), errors


def _data() -> ReportData:
    scenario = {
        "scenario_id": "demo_default",
        "title": "Demo default",
        "status": "official",
        "description": "Official <b>scenario</b>.",
        "content_hash": "a" * 64,
        "assumption_hashes": {"score_method_v0": "b" * 64},
        "sets": {"score_method": "score_method_v0"},
        "method_switches": {"score_method.municipality_missing_input_policy": "null_component"},
        "overrides": {},
        "owners": ["oleg"],
        "approved_by": None,
        "approved_on": None,
        "score_run_id": "run-1",
        "opportunity_run_id": None,
        "valuation_run_id": None,
    }
    other = {**scenario, "scenario_id": "spec_v0", "title": "Spec", "status": "exploratory", "method_switches": {}}
    row = {
        "entity_id": "dk.municipality.lolland",
        "name": "Lolland",
        "kind": "municipality",
        "zone_id": "dk.zone.dk2",
        "congestion_score": 53.2,
        "rank": 81,
        "revenue_low_eur_per_mw_year": 100000.0,
        "revenue_high_eur_per_mw_year": 120000.0,
        "score_delta": 0.0,
        "rank_delta": 0,
        "score_flags": [],
        "is_benchmark": True,
    }
    comparison = [
        {**row, "scenario_id": "demo_default"},
        {**row, "scenario_id": "spec_v0", "rank": 83, "rank_delta": 2, "score_flags": ["no_station_data"]},
    ]
    register = [
        {
            "set_id": "score_method_v0",
            "set_kind": "score_method",
            "set_status": "proposed",
            "parameter": "municipality_missing_input_policy",
            "value": None,
            "proposed": "renormalise",
            "note": "a | b",
            "owners": ["oleg"],
            "status": "proposed_only",
            "scenarios": ["demo_default", "spec_v0"],
            "scenario_values": {"demo_default": "null_component"},
            "content_hash": "b" * 64,
            "source_path": "assumptions/score_method_v0.yaml",
        }
    ]
    method = {
        "method_id": "municipality_site_score",
        "version": 1,
        "title": "Municipality site score",
        "status": "provisional",
        "owner": ["oleg"],
        "inputs": ["f7_substation_saturation"],
        "parameters": ["score_method.municipality_missing_input_policy"],
        "outputs": ["feat.score"],
        "known_biases": ["Many sites tie."],
        "body": "**Formula.** Sum of contributions.\n",
    }
    return ReportData(datetime(2026, 10, 18, 12, tzinfo=UTC), [scenario, other], comparison, register, [method])


def test_report_renders_register_cards_and_comparison() -> None:
    md = render_markdown(_data())
    for heading in ("## 1. Scenarios", "## 2. Scenario comparison", "## 3. Assumption register", "## 4. Method cards"):
        assert heading in md
    assert "Official scenario: **Demo default**" in md and "a \\| b" in md
    assert "| Lolland (benchmark) | dk.zone.dk2 | 53.2 (#81) | 53.2 (#83) | +0.0 points, 2 places lower |" in md
    assert "no_station_data (spec_v0)" in md and "100,000 – 120,000" in md
    page = render_html(md)
    assert page.startswith("<!doctype html>") and "<table>" in page and "<script" not in page
    assert "<b>scenario</b>" not in page and "&lt;b&gt;scenario&lt;/b&gt;" in page  # raw HTML is escaped
    assert "<strong>Formula.</strong>" in page
