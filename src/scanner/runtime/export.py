"""Static export of the serving views (WP1.11), the fallback if the live API fails during the demo.

Every view in the `serving` schema is written to `exports/`: map views as GeoJSON FeatureCollections
(GEO_COLUMNS names each one's geometry column; the other columns become properties, `id` is the entity),
the others as JSON `{"rows": [...]}`. `exports/index.json` lists the files with their row counts, the
generation time (UTC), the latest derived-data runs (serving.latest_runs) and the distinct labels found in
the rows. The queries run as role `anon`, so the files hold exactly what the public API serves: public rows
only, and only views anon may read. Contract: docs/ui-contract.md.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg

from scanner import REPO_ROOT
from scanner.settings import get_settings

GEO_COLUMNS = {"zone_map": "geometry", "substation_map": "geometry", "ranked_sites": "point"}
LABEL_COLUMNS = ("labels", "score_labels", "revenue_labels", "caveat", "f7_caveat")


def _json(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def _write(path: Path, payload: Any) -> None:
    text = json.dumps(payload, default=_json, ensure_ascii=False, separators=(",", ":"))
    path.write_text(text + "\n", encoding="utf-8", newline="\n")


def feature_collection(rows: list[dict[str, Any]], geometry: str) -> dict[str, Any]:
    """A GeoJSON FeatureCollection; a row without geometry stays in it as an unlocated feature (RFC 7946)."""
    features = []
    for r in rows:
        props = {k: v for k, v in r.items() if k != geometry}
        features.append({"type": "Feature", "id": r.get("entity_id"), "geometry": r.get(geometry), "properties": props})
    return {"type": "FeatureCollection", "features": features}


def labels_in(rows: list[dict[str, Any]]) -> set[str]:
    found: set[str] = set()
    for r in rows:
        for col in LABEL_COLUMNS:
            v = r.get(col)
            found.update(v if isinstance(v, list) else [v] if isinstance(v, str) else [])
    return found


def export_static(out_dir: Path = REPO_ROOT / "exports", database_url: str | None = None) -> list[str]:
    """Write every serving view and index.json to out_dir; return the paths written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    files: list[dict[str, Any]] = []
    labels: set[str] = set()
    with psycopg.connect(database_url or get_settings().database_url) as conn:
        conn.execute("set local role anon")
        conn.execute("set local time zone 'UTC'")
        views = [
            v
            for (v,) in conn.execute(
                "select table_name from information_schema.views where table_schema = 'serving' order by 1"
            )
        ]
        runs: list[dict[str, Any]] = []
        for view in views:
            cur = conn.execute(f'select * from serving."{view}"')
            cols = [d.name for d in cur.description or []]
            rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
            labels |= labels_in(rows)
            if view == "latest_runs":
                runs = rows
            if view in GEO_COLUMNS:
                path, fmt, payload = out_dir / f"{view}.geojson", "geojson", feature_collection(rows, GEO_COLUMNS[view])
            else:
                path, fmt, payload = out_dir / f"{view}.json", "json", {"rows": rows}
            _write(path, payload)
            written.append(path)
            files.append({"view": f"serving.{view}", "path": path.name, "format": fmt, "rows": len(rows)})
    index = {
        "generated_at": datetime.now(UTC).isoformat(),
        "contract": "docs/ui-contract.md",
        "files": files,
        "runs": runs,
        "labels": sorted(labels),
    }
    _write(out_dir / "index.json", index)
    written.append(out_dir / "index.json")
    return [str(p) for p in written]


if __name__ == "__main__":
    for p in export_static():
        print(p)
