"""Dagster code location `ingestion` (workspace.yaml): ingest assets, snapshots, the capacity map, the curated
loader and their schedules.

It imports nothing from the analytics location (features, scores, valuation, scanner.analytics_definitions), so
a broken analytics import cannot stop the ingest schedules: each location loads in its own process
(tests/test_definitions.py).
"""

from __future__ import annotations

from dagster import Definitions

from scanner.assets.capacity import CAPACITY_ASSETS, CAPACITY_SCHEDULES
from scanner.assets.catchup import CATCH_UP_JOBS, CATCH_UP_SENSORS
from scanner.assets.curated import CURATED_ASSETS
from scanner.assets.ingest import build_ingest_defs
from scanner.assets.snapshots import build_snapshot_defs

_ingest_assets, _ingest_schedules = build_ingest_defs()
_snapshot_assets, _snapshot_schedules = build_snapshot_defs()

defs = Definitions(
    assets=[*CURATED_ASSETS, *_ingest_assets, *_snapshot_assets, *CAPACITY_ASSETS],
    jobs=CATCH_UP_JOBS,
    schedules=[*_ingest_schedules, *_snapshot_schedules, *CAPACITY_SCHEDULES],
    sensors=CATCH_UP_SENSORS,
)
