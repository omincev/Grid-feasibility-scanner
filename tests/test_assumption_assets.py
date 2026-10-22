"""Assumption files in the Dagster lineage (scanner.assets.assumptions): observable source assets whose data
version is the file's content hash, the scenario partitions, staleness after a change, and the sensor."""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from conftest import insert_scenario
from dagster import (
    AssetKey,
    BackfillPolicy,
    DagsterInstance,
    Definitions,
    MaterializeResult,
    asset,
    build_sensor_context,
    define_asset_job,
    instance_for_test,
)
from dagster._core.definitions.data_version import CachingStaleStatusResolver, StaleStatus
from dagster._core.loader import LoadingContextForTest
from psycopg.types.json import Jsonb

from scanner import REPO_ROOT
from scanner.analytics_definitions import defs
from scanner.assets.assumptions import (
    SCENARIO_PARTITIONS,
    SCENARIOS_KEY,
    assumption_assets,
    build_sensor,
    current_hashes,
    file_hash,
    method_key,
    observe_job,
    set_key,
    stale_copies,
)
from scanner.assets.compute import every_scenario
from scanner.core.curated import read_curated
from scanner.core.methods import load_method_cards
from scanner.core.registry import load_assumptions, load_scenarios


def _key(s: str) -> AssetKey:
    return AssetKey.from_user_string(s)


def test_every_assumption_file_is_observed_with_the_hash_load_curated_stores() -> None:
    graph = defs.resolve_asset_graph()
    for set_id in load_assumptions(REPO_ROOT):
        assert graph.get(set_key(set_id)).is_observable
    for method_id in load_method_cards(REPO_ROOT):
        assert graph.get(method_key(method_id)).is_observable
    scenarios = graph.get(SCENARIOS_KEY)
    assert scenarios.is_observable and scenarios.partitions_def == SCENARIO_PARTITIONS
    loaded = {
        u.path: u.content_hash for u in read_curated(REPO_ROOT) if u.folder in ("assumptions", "scenarios", "methods")
    }
    assert current_hashes(REPO_ROOT) == loaded


def test_outputs_depend_on_the_files_they_use() -> None:
    graph = defs.resolve_asset_graph()

    def parents(key: str) -> set[str]:
        return {k.to_user_string() for k in graph.get(_key(key)).parent_keys}

    congestion = parents("scores/congestion")
    assert {"assumptions/score_weights_v0", "assumptions/score_method_v0", "assumptions/scenarios"} <= congestion
    assert {"methods/congestion_score", "methods/municipality_site_score", "curated/analytics_load"} <= congestion
    assert {"assumptions/opportunity_rules_v0", "methods/opportunity_rules"} <= parents("scores/opportunity_types")
    valuation = parents("valuation/bess_return_range_v0")
    assert {
        "assumptions/bess_mvp",
        "methods/bess_v0_daily_lp_day_ahead",
        "ingest/eds/dk_eds_dayaheadprices",
    } <= valuation
    for scenario_asset in ("scores/congestion", "scores/opportunity_types", "valuation/bess_return_range_v0"):
        assert graph.get(_key(scenario_asset)).partitions_def == SCENARIO_PARTITIONS
    for feature in (
        "f1_price_spread",
        "f2_curtailment",
        "f4_surplus",
        "f5_balancing_activation",
        "f7_substation_saturation",
    ):
        node = graph.get(_key(f"features/{feature}"))
        ups = {k.to_user_string() for k in node.parent_keys}
        assert not node.is_partitioned, "features are scenario-independent"
        assert f"methods/{feature}" in ups and not any(u.startswith("assumptions/") for u in ups)
        assert any(u.split("/")[0] in ("ingest", "capacity") for u in ups), f"{feature} reads no ingested data"
    assert "ingest/eds/dk_eds_generationprodtypeexchange" in parents("features/f4_surplus")


def _repo_copy(tmp_path: Path) -> Path:
    shutil.copytree(REPO_ROOT / "assumptions", tmp_path / "assumptions")
    shutil.copytree(REPO_ROOT / "docs" / "methods", tmp_path / "docs" / "methods")
    return tmp_path


def _touch(path: Path) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write("# edited\n")


def _status(instance: DagsterInstance, d: Definitions, key: str, partition: str | None = None) -> StaleStatus:
    resolver = CachingStaleStatusResolver(instance, d.resolve_asset_graph(), LoadingContextForTest(instance))
    return resolver.get_status(_key(key), partition)


def test_a_changed_file_shows_the_assets_that_use_it_as_stale(tmp_path: Path) -> None:
    root = _repo_copy(tmp_path)
    observables = assumption_assets(root)

    @asset(
        key=_key("scores/test_score"),
        deps=[SCENARIOS_KEY, set_key("score_weights_v0")],
        partitions_def=SCENARIO_PARTITIONS,
        backfill_policy=BackfillPolicy.single_run(),
    )
    def score() -> MaterializeResult:
        return MaterializeResult()

    @asset(key=_key("features/test_feature"), deps=[method_key("f1_price_spread")])
    def feature() -> MaterializeResult:
        return MaterializeResult()

    observe = observe_job(observables)
    compute = define_asset_job("compute", selection=[score.key, feature.key])
    d = Definitions(assets=[*observables, score, feature], jobs=[observe, compute])
    with instance_for_test() as instance:
        instance.add_dynamic_partitions(SCENARIO_PARTITIONS.name, ["demo_default", "spec_v0"])
        assert d.resolve_job_def(observe.name).execute_in_process(instance=instance).success
        tags = every_scenario(["demo_default", "spec_v0"])
        assert d.resolve_job_def("compute").execute_in_process(instance=instance, tags=tags).success
        assert {_status(instance, d, "scores/test_score", p) for p in ("demo_default", "spec_v0")} == {
            StaleStatus.FRESH
        }

        _touch(root / "assumptions" / "scenarios" / "spec_v0.yaml")
        d.resolve_job_def(observe.name).execute_in_process(instance=instance)
        assert _status(instance, d, "scores/test_score", "spec_v0") == StaleStatus.STALE
        assert _status(instance, d, "scores/test_score", "demo_default") == StaleStatus.FRESH
        assert _status(instance, d, "features/test_feature") == StaleStatus.FRESH

        _touch(root / "assumptions" / "score_weights_v0.yaml")
        _touch(root / "docs" / "methods" / "f1_price_spread.md")
        d.resolve_job_def(observe.name).execute_in_process(instance=instance)
        assert _status(instance, d, "scores/test_score", "demo_default") == StaleStatus.STALE
        assert _status(instance, d, "features/test_feature") == StaleStatus.STALE


def test_sensor_keeps_the_scenario_partitions_and_observes_after_a_change(tmp_path: Path) -> None:
    root = _repo_copy(tmp_path)
    sensor = build_sensor(observe_job(assumption_assets(root)), root)
    with instance_for_test() as instance:

        def tick(cursor: str | None) -> Any:
            return sensor(build_sensor_context(instance=instance, cursor=cursor))

        first = tick(None)
        (add,) = first.dynamic_partitions_requests
        assert sorted(add.partition_keys) == sorted(load_scenarios(REPO_ROOT)) and len(first.run_requests) == 1
        instance.add_dynamic_partitions(SCENARIO_PARTITIONS.name, list(add.partition_keys))
        quiet = tick(first.cursor)
        assert quiet.run_requests == [] and quiet.dynamic_partitions_requests == []
        _touch(root / "docs" / "methods" / "congestion_score.md")
        assert len(tick(first.cursor).run_requests) == 1
        (root / "assumptions" / "scenarios" / "spec_v0.yaml").unlink()
        gone = tick(first.cursor)
        assert [list(r.partition_keys) for r in gone.dynamic_partitions_requests] == [["spec_v0"]]


@pytest.fixture()
def db_url() -> Iterator[str]:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    yield url
    with psycopg.connect(url) as conn:
        conn.execute("delete from ref.scenario where scenario_id like 'test\\_stale\\_%'")
        conn.execute("delete from ref.assumption_set where set_id like 'test\\_stale\\_%'")


@pytest.mark.db
def test_stale_copies_names_the_files_ref_holds_older_versions_of(db_url: str, tmp_path: Path) -> None:
    tag = f"test_stale_{uuid.uuid4().hex[:6]}"
    scenario = tmp_path / "assumptions" / "scenarios" / f"{tag}.yaml"
    weights = tmp_path / "assumptions" / f"{tag}_weights.yaml"
    scenario.parent.mkdir(parents=True)
    scenario.write_text("id: x\n", encoding="utf-8")
    weights.write_text("id: y\n", encoding="utf-8")
    with psycopg.connect(db_url) as conn:
        conn.execute(
            "insert into ref.assumption_set (set_id, kind, status, owners, parameters, source_path, content_hash) "
            "values (%s, 'test', 'proposed', '{owner}', %s, 'test', %s)",
            (f"{tag}_weights", Jsonb({}), file_hash(weights)),
        )
        insert_scenario(conn, tag, {"score_weights": f"{tag}_weights"})
        conn.execute("update ref.scenario set content_hash = %s where scenario_id = %s", (file_hash(scenario), tag))
        assert stale_copies(conn, tmp_path, tag, ["score_weights"]) == []
        _touch(weights)
        assert stale_copies(conn, tmp_path, tag, ["score_weights"]) == [f"assumptions/{tag}_weights.yaml"]
        _touch(scenario)
        assert stale_copies(conn, tmp_path, tag, []) == [f"assumptions/scenarios/{tag}.yaml"]
        assert stale_copies(conn, tmp_path, "test_stale_missing", []) == [
            "assumptions/scenarios/test_stale_missing.yaml (no such file)"
        ]
        conn.rollback()
