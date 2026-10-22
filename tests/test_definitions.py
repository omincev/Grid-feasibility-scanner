"""The Dagster code locations (workspace.yaml): what each defines, the lineage between ingestion and analytics,
that a broken analytics import cannot stop the ingestion location or its schedules, and that the `ops` alert
location stands alone."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import yaml
from dagster import AssetKey

from scanner import REPO_ROOT

INGESTION_ONLY = ("ingest", "snapshots", "capacity")


def _keys(defs) -> set[str]:  # type: ignore[no-untyped-def]
    return {k.to_user_string() for k in defs.resolve_asset_graph().get_all_asset_keys()}


def test_ingestion_location() -> None:
    from scanner.ingestion_definitions import defs

    graph = defs.resolve_asset_graph()
    keys = _keys(defs)
    assert {"ingest/eds/dk_eds_dayaheadprices", "ingest/eds/dk_eds_elspotprices", "curated/files"} <= keys
    assert "capacity/kapacitetskort" in keys
    assert not any(k.split("/")[0] in ("features", "scores", "valuation", "serving", "assumptions") for k in keys)
    assert all(graph.get(k).is_materializable for k in graph.get_all_asset_keys())
    names = {s.name for s in defs.schedules or []}
    assert any(n.startswith("schedule_dk_eds_imbalanceprice") for n in names)
    assert "schedule_capacity_kapacitetskort" in names and "schedule_compute_daily" not in names
    assert not any("elspotprices" in n for n in names), "retired datasets are backfill only"


def test_analytics_location_reads_ingestion_assets_as_external_specs() -> None:
    from scanner.analytics_definitions import defs
    from scanner.ingestion_definitions import defs as ingestion

    graph = defs.resolve_asset_graph()
    keys = _keys(defs)
    assert {"features/f1_price_spread", "scores/congestion", "serving/static_export"} <= keys
    external = {k.to_user_string() for k in graph.get_all_asset_keys() if graph.get(k).is_external}
    upstream = {k for k in external if k.split("/")[0] in INGESTION_ONLY}
    assert upstream and upstream <= _keys(ingestion), "every upstream spec is an ingestion asset, same key"
    assert {k for k in keys if k.split("/")[0] in INGESTION_ONLY} == upstream, "analytics defines no ingestion asset"
    assert AssetKey(["serving", "static_export"]) in graph.get_all_asset_keys()
    assert {s.name for s in defs.schedules or []} == {"schedule_compute_daily", "schedule_prune_feature_runs"}
    assert {s.name for s in defs.sensors or []} == {"sensor_assumption_files"}


def test_workspace_loads_every_location() -> None:
    workspace = yaml.safe_load((REPO_ROOT / "workspace.yaml").read_text(encoding="utf-8"))
    modules = {e["python_module"]["location_name"]: e["python_module"]["module_name"] for e in workspace["load_from"]}
    assert modules == {
        "ingestion": "scanner.ingestion_definitions",
        "analytics": "scanner.analytics_definitions",
        "ops": "scanner.ops_definitions",
    }


def test_ops_location_holds_only_the_alert_sensors() -> None:
    from scanner.ops_definitions import defs

    assert {s.name for s in defs.sensors or []} == {"sensor_run_failure_alert", "sensor_health_alert"}
    assert not defs.schedules and not defs.jobs and not _keys(defs)


# The ops location must load when the ingestion and analytics modules cannot, so alerts outlive a broken import.
_OPS_ALONE = textwrap.dedent(
    """
    import sys
    from scanner.ops_definitions import defs

    loaded = [m for m in sys.modules if m.startswith(("scanner.assets", "scanner.features", "scanner.scores",
              "scanner.valuation", "scanner.adapters", "scanner.ingestion_definitions",
              "scanner.analytics_definitions"))]
    assert not loaded, loaded
    print(len(defs.sensors))
    """
)


def test_the_ops_location_imports_no_ingestion_or_analytics_module(tmp_path: Path) -> None:
    script = tmp_path / "ops_alone.py"
    script.write_text(_OPS_ALONE, encoding="utf-8", newline="\n")
    out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=300, cwd=REPO_ROOT)
    assert out.returncode == 0, out.stderr[-3000:]
    assert out.stdout.strip().splitlines()[-1] == "2"


# Run in a fresh interpreter: every analytics module raises on import, then the ingestion location loads.
_BROKEN_ANALYTICS = textwrap.dedent(
    """
    import importlib.abc, sys

    BROKEN = ("scanner.analytics_definitions", "scanner.features", "scanner.scores", "scanner.valuation",
              "scanner.assets.assumptions", "scanner.assets.compute", "scanner.assets.features",
              "scanner.assets.scores", "scanner.assets.valuation", "scanner.assets.serving")

    class Broken(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name in BROKEN or name.startswith(tuple(b + "." for b in BROKEN)):
                raise ImportError(f"simulated broken analytics module {name}")
            return None

    sys.meta_path.insert(0, Broken())
    from scanner.ingestion_definitions import defs

    names = [s.name for s in defs.schedules]
    defs.resolve_asset_graph()
    assert any(n.startswith("schedule_dk_eds_") for n in names), names
    print(len(names))
    """
)


def test_a_broken_analytics_import_leaves_ingestion_and_its_schedules_loading(tmp_path: Path) -> None:
    script = tmp_path / "broken_analytics.py"
    script.write_text(_BROKEN_ANALYTICS, encoding="utf-8", newline="\n")
    out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=300, cwd=REPO_ROOT)
    assert out.returncode == 0, out.stderr[-3000:]
    assert int(out.stdout.strip().splitlines()[-1]) > 0
