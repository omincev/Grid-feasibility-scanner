"""The daily compute job and schedule (scanner.assets.compute) and schedule defaults (scanner.assets.automation)."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from dagster import AssetKey, DefaultScheduleStatus, RunRequest, SkipReason, build_schedule_context, instance_for_test

from scanner.analytics_definitions import defs
from scanner.assets import assumptions, features, scores, serving, valuation
from scanner.assets.compute import every_scenario, schedule_compute_daily
from scanner.assets.features import MVP_FEATURE_ASSETS as MVP
from scanner.assets.ingest import build_ingest_defs
from scanner.assets.snapshots import build_snapshot_defs
from scanner.runtime.context import RepoState
from scanner.settings import get_settings

COMPUTE = {
    "curated/analytics_load",
    "features/f1_price_spread",
    "features/f2_curtailment",
    "features/f4_surplus",
    "features/f5_balancing_activation",
    "features/f7_substation_saturation",
    "scores/congestion",
    "scores/opportunity_types",
    "valuation/bess_return_range_v0",
    "serving/static_export",
}
# Refreshed outside the compute job: by their own schedules in the ingestion location, or (assumption files and
# method cards) observed by sensor_assumption_files. The compute job only reads them.
SOURCE_SIDE = ("ingest", "snapshots", "capacity", "assumptions", "methods")
SCENARIOS = ["demo_default", "spec_v0"]


def _key(s: str) -> AssetKey:
    return AssetKey.from_user_string(s)


def test_compute_job_selects_the_built_compute_assets() -> None:
    job = defs.resolve_job_def("job_compute")
    assert {k.to_user_string() for k in job.asset_layer.selected_asset_keys} == COMPUTE


def test_compute_job_has_every_compute_input_and_runs_in_dependency_order() -> None:
    graph = defs.resolve_asset_graph()
    job = defs.resolve_job_def("job_compute")
    order = [n.name for n in job.graph.nodes_in_topological_order]
    position = {_key(k): order.index("__".join(_key(k).path)) for k in COMPUTE}
    for key in position:
        for up in graph.get(key).parent_keys:
            if up in position:
                assert position[up] < position[key], f"{up} must run before {key}"
            else:
                assert up.path[0] in SOURCE_SIDE, f"{key} reads {up}, which nothing refreshes"
    # The curated load runs first; the weekly capacity map reaches the scores through F7.
    load = position[_key("curated/analytics_load")]
    assert load == min(position.values())
    assert _key("capacity/kapacitetskort") in graph.get(_key("features/f7_substation_saturation")).parent_keys
    chain = ["features/f7_substation_saturation", "scores/congestion", "scores/opportunity_types"]
    assert [position[_key(k)] for k in chain] == sorted(position[_key(k)] for k in chain)
    export = position[_key("serving/static_export")]
    assert export == max(position.values())
    assert position[_key("valuation/bess_return_range_v0")] < export


def test_compute_schedule_runs_every_scenario_after_the_day_ahead_ingests() -> None:
    s = defs.resolve_schedule_def("schedule_compute_daily")
    assert s.job_name == "job_compute"
    assert (s.cron_schedule, s.execution_timezone) == ("30 15 * * *", "Europe/Copenhagen")
    with instance_for_test() as instance:
        assert isinstance(schedule_compute_daily(build_schedule_context(instance=instance)), SkipReason)
        instance.add_dynamic_partitions("scenario", SCENARIOS)
        request = schedule_compute_daily(build_schedule_context(instance=instance))
    assert isinstance(request, RunRequest) and request.partition_key is None
    assert request.tags == every_scenario(SCENARIOS)


@pytest.fixture
def fresh_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("env", "status"), [("true", DefaultScheduleStatus.RUNNING), ("false", DefaultScheduleStatus.STOPPED)]
)
def test_schedules_run_by_default_only_where_the_deployment_says_so(
    monkeypatch: pytest.MonkeyPatch, fresh_settings: None, state: RepoState, env: str, status: DefaultScheduleStatus
) -> None:
    monkeypatch.setenv("SCANNER_SCHEDULES_DEFAULT_RUNNING", env)
    schedules = [*build_ingest_defs(state)[1], *build_snapshot_defs()[1]]
    assert schedules
    assert {s.default_status for s in schedules} == {status}


class _NoConnection:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> None:
        return None


def test_one_compute_run_loads_first_computes_features_once_and_every_scenario(monkeypatch: pytest.MonkeyPatch) -> None:
    """job_compute over the whole scenario range, with the runtime functions replaced by recorders."""
    calls: list[tuple[str, str | None]] = []

    def outcome(name: str, scenario: str | None = None) -> Any:
        return SimpleNamespace(
            feature=name, name=name, run_id="r", status="ok", rows_written=1, notes=[], scenario_id=scenario
        )

    def record(name: str) -> Any:
        def run(*args: Any, scenario: str | None = None, **kwargs: Any) -> Any:
            calls.append((name, scenario))
            return outcome(name, scenario)

        return run

    def run_features(keys: list[str], **kwargs: Any) -> list[Any]:
        calls.append((keys[0], None))
        return [outcome(keys[0])]

    def load(*args: Any) -> Any:
        calls.append(("load", None))
        return SimpleNamespace(changes=0, text=lambda: "")

    def export(**kwargs: Any) -> list[str]:
        calls.append(("export", None))
        return []

    monkeypatch.setattr(assumptions, "load", load)
    monkeypatch.setattr(assumptions, "_loaded_version", lambda url: "v")
    monkeypatch.setattr(features, "run_features", run_features)
    monkeypatch.setattr(scores, "psycopg", SimpleNamespace(connect=lambda *a, **k: _NoConnection()))
    monkeypatch.setattr(scores, "scenario_ids", lambda conn: ["demo_default", "spec_v0"])  # official first
    monkeypatch.setattr(scores, "stale_copies", lambda *a: [])
    monkeypatch.setattr(scores, "run_congestion_scores", record("congestion"))
    monkeypatch.setattr(scores, "run_opportunity_types", record("opportunity"))
    monkeypatch.setattr(valuation, "run_bess_v0", record("valuation"))
    monkeypatch.setattr(serving, "export_static", export)
    monkeypatch.setattr(serving, "write_report", lambda **k: [])

    with instance_for_test() as instance:
        instance.add_dynamic_partitions("scenario", ["spec_v0", "demo_default"])
        request = schedule_compute_daily(build_schedule_context(instance=instance))
        assert isinstance(request, RunRequest)
        result = defs.resolve_job_def("job_compute").execute_in_process(instance=instance, tags=request.tags)
        assert result.success
        materialized = {
            (e.asset_key.to_user_string(), e.partition)
            for e in result.get_asset_materialization_events()
            if e.asset_key is not None
        }
    assert calls[0] == ("load", None) and calls[-1] == ("export", None)
    assert sorted(name for name, _ in calls if name.startswith("f")) == sorted(a.key.path[-1] for a in MVP)
    for step in ("congestion", "opportunity", "valuation"):
        assert [s for name, s in calls if name == step] == ["demo_default", "spec_v0"], "every scenario, official first"
    for key in ("scores/congestion", "scores/opportunity_types", "valuation/bess_return_range_v0"):
        assert {p for k, p in materialized if k == key} == {"demo_default", "spec_v0"}
    assert ("serving/static_export", None) in materialized
