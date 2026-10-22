"""Dagster code location `analytics` (workspace.yaml): what is computed, from what, with which assumptions.

Features, scores, opportunity types, valuation and the serving export; every assumption set, scenario file
and method card as an observable source asset (scanner.assets.assumptions); scores, opportunities and
valuation partitioned by scenario; the daily compute job and its schedule (scanner.assets.compute).

The ingestion-owned assets these read (ingest, capacity map) are declared here as external asset specs with
the same keys, so the UI shows the lineage end to end; their definitions and schedules live in
scanner.ingestion_definitions, which this location's import failures cannot reach.
"""

from __future__ import annotations

from collections.abc import Sequence

from dagster import AssetKey, AssetsDefinition, AssetSpec, Definitions, SourceAsset

from scanner.assets.assumptions import assumption_assets, build_sensor, curated_load_asset, observe_job
from scanner.assets.compute import COMPUTE_SCHEDULES, compute_job
from scanner.assets.estimates import ESTIMATE_ASSETS
from scanner.assets.features import FEATURE_ASSETS
from scanner.assets.forecasts import FORECAST_ASSETS
from scanner.assets.maintenance import MAINTENANCE_JOBS, MAINTENANCE_SCHEDULES
from scanner.assets.scores import SCORE_ASSETS
from scanner.assets.serving import SERVING_ASSETS
from scanner.assets.valuation import VALUATION_ASSETS


def upstream_specs(assets: Sequence[AssetsDefinition | SourceAsset]) -> list[AssetSpec]:
    """External specs for every dependency no asset here defines: the ingestion location's assets."""
    own = {k for a in assets for k in (a.keys if isinstance(a, AssetsDefinition) else [a.key])}
    deps: set[AssetKey] = {d for a in assets if isinstance(a, AssetsDefinition) for d in a.dependency_keys}
    return [
        AssetSpec(k, description="Defined in the ingestion code location (scanner.ingestion_definitions).")
        for k in sorted(deps - own, key=lambda k: k.to_user_string())
    ]


_observables = assumption_assets()
_assets: list[AssetsDefinition | SourceAsset] = [
    *_observables,
    curated_load_asset([a.key for a in _observables]),
    *FEATURE_ASSETS,
    *SCORE_ASSETS,
    *VALUATION_ASSETS,
    *ESTIMATE_ASSETS,
    *FORECAST_ASSETS,
    *SERVING_ASSETS,
]
_observe_job = observe_job(_observables)

defs = Definitions(
    assets=[*_assets, *upstream_specs(_assets)],
    jobs=[compute_job, _observe_job, *MAINTENANCE_JOBS],
    schedules=[*COMPUTE_SCHEDULES, *MAINTENANCE_SCHEDULES],
    sensors=[build_sensor(_observe_job)],
)
