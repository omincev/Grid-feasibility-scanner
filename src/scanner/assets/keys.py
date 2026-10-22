"""Asset keys shared by the two code locations (workspace.yaml).

The analytics location declares the ingestion-owned keys it reads as external asset specs with these same
keys, so the lineage stays connected in one UI. This module imports no runtime or adapter code: a broken
adapter must not stop analytics from loading, and a broken analytics module must not stop ingestion.
"""

from __future__ import annotations

from dagster import AssetKey

from scanner.core.manifest import Manifest
from scanner.core.registry import Snapshot

# Curated loader of the ingestion location (scanner.assets.curated).
CURATED_KEY = AssetKey(["curated", "files"])
# Shared Danish capacity map (scanner.assets.capacity).
CAPACITY_MAP_KEY = AssetKey(["capacity", "kapacitetskort"])
# Codebook sources whose entries no manifest ingests: the asset that writes them (as core.fact rows).
FACT_WRITERS: dict[str, AssetKey] = {"kapacitetskort": CAPACITY_MAP_KEY}


def ingest_key(m: Manifest) -> AssetKey:
    return AssetKey(["ingest", m.source_short, m.asset_name])


def snapshot_key(s: Snapshot) -> AssetKey:
    return AssetKey(["snapshots", s.id.replace(".", "__").replace("-", "_")])


def ingested(m: Manifest) -> bool:
    """Manifests that get an ingest asset (scanner.assets.ingest.build_ingest_defs)."""
    return m.status not in ("draft", "retired")
