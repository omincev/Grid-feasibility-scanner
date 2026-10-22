"""Capacity-map loader (WP1.5): Kapacitetskortet_V2 -> ref.entity, ref.entity_geometry, core.fact.

One run: resolve the layer ids by name from the service index (ids are positional), fetch each layer as
GeoJSON in EPSG:4326, paging past maxRecordCount, and keep every response in the raw store and as a
core.source_document. Upsert the DSO licence areas (Netselskabsgraenser) as dso_area entities with
their polygons, join every station point against the zone, municipality and licence-area polygons in
ref.entity_geometry, upsert the stations (scanner.adapters.kapacitetskort.stations for IDs and rules)
with their points, and append the facts whose value changed (scanner.core.db.write_facts).

Records: one ops.ingest_run row per run (source kapacitetskort, dataset Kapacitetskortet_V2, no
partition or window: the source has no time axis, only publication dates per station). Machine-loaded
entities carry source_path 'runtime:kapacitetskort' and are never in git; entities of this source
that a run no longer finds get attributes.listed = false. A station whose point is not in exactly one
zone, municipality or licence area keeps that attribute null (the candidates are listed), is reported,
and one `human` bead asks for a decision.

Transactions: every block uses conn.transaction(), so with an autocommit connection (the CLI and the
asset) the run row commits before the data and survives a failure; inside a caller's transaction
(tests) everything nests as savepoints and can be rolled back.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx
import psycopg
from psycopg.types.json import Jsonb

from scanner.adapters.kapacitetskort.stations import (
    BOUNDARY_LAYER,
    BOUNDARY_SERVICE_URL,
    COUNTRY,
    LAYERS,
    SERVICE_URL,
    SOURCE,
    Layer,
    Location,
    ParseError,
    Quirk,
    StationRow,
    build_stations,
    features,
    parse_dso_areas,
    parse_layer,
)
from scanner.core import db
from scanner.core.raw_store import RawStore
from scanner.core.registry import country_entity_id
from scanner.runtime.provenance import code_version
from scanner.settings import get_settings

DATASET = "Kapacitetskortet_V2"
SOURCE_PATH = f"runtime:{SOURCE}"
MAX_PAGES = 100

# (url, params) -> (status, content type, body)
Fetch = Callable[[str, Mapping[str, str]], tuple[int, str, bytes]]


def http_fetch(pause_s: float = 0.5) -> Fetch:
    """Sequential GETs with the settings User-Agent and a short pause between requests."""
    s = get_settings()

    def fetch(url: str, params: Mapping[str, str]) -> tuple[int, str, bytes]:
        with httpx.Client(timeout=s.http_timeout_s, headers={"User-Agent": s.user_agent}) as c:
            r = c.get(url, params=dict(params))
        time.sleep(pause_s)
        return r.status_code, r.headers.get("content-type", ""), r.content

    return fetch


@dataclass
class _Captures:
    """Fetches, stores raw and remembers each response for core.source_document."""

    fetch: Fetch
    store: RawStore
    run_id: uuid.UUID
    documents: list[dict[str, Any]] = field(default_factory=list)

    def json(self, url: str, params: Mapping[str, str], label: str) -> tuple[Any, str]:
        status, ctype, content = self.fetch(url, params)
        if status != 200:
            raise RuntimeError(f"{label}: HTTP {status} from {url}")
        fetched_at = datetime.now(UTC)
        seq = len(self.documents)
        full_url = f"{url}?{urlencode(dict(params))}"
        ref = self.store.put(
            source=SOURCE,
            dataset=label,
            run_id=str(self.run_id),
            seq=seq,
            content=content,
            ext="json",
            fetched_at=fetched_at,
            meta={"url": full_url, "status": status, "content_type": ctype},
        )
        doc_id = f"{SOURCE}:{label}:{self.run_id}:{seq}"
        self.documents.append(
            {
                "document_id": doc_id,
                "url": full_url,
                "title": label,
                "fetched_at": fetched_at,
                "sha256": ref.sha256,
                "raw_uri": ref.uri,
                "content_type": ctype or "application/json",
            }
        )
        try:
            data = json.loads(content)
        except ValueError as exc:
            raise ParseError(f"{label}: response is not JSON") from exc
        if isinstance(data, dict) and "error" in data:
            raise RuntimeError(f"{label}: service error {data['error']}")
        return data, doc_id

    def fetched_at(self, document_id: str) -> datetime:
        return next(d["fetched_at"] for d in self.documents if d["document_id"] == document_id)

    def layer(self, service: str, index: Any, name: str) -> list[tuple[Any, str]]:
        """Every page of a layer, by name; pages while the service reports exceededTransferLimit. A layer
        without any feature raises ParseError (ADR 0013: zero rows where rows are expected is hard), before
        the write would mark every entity of the layer as no longer listed."""
        layer_id = layer_ids(index).get(name)
        if layer_id is None:
            raise ParseError(f"layer {name} not in {service}; layers: {sorted(layer_ids(index))}")
        page_size = int(index.get("maxRecordCount") or 1000)
        pages: list[tuple[Any, str]] = []
        offset = 0
        for _ in range(MAX_PAGES):
            params = {
                "where": "1=1",
                "outFields": "*",
                "outSR": "4326",
                "orderByFields": "OBJECTID",
                "resultOffset": str(offset),
                "resultRecordCount": str(page_size),
                "f": "geojson",
            }
            fc, doc_id = self.json(f"{service}/{layer_id}/query", params, name)
            pages.append((fc, doc_id))
            n = len(features(fc))
            if not (fc.get("properties") or {}).get("exceededTransferLimit") or n == 0:
                if not any(features(page) for page, _ in pages):
                    raise ParseError(f"{name}: zero features")
                return pages
            offset += n
        raise RuntimeError(f"{name}: more than {MAX_PAGES} pages")


def layer_ids(index: Any) -> dict[str, int]:
    """Layer name -> id from a FeatureServer's ?f=json."""
    if not isinstance(index, dict) or not isinstance(index.get("layers"), list):
        raise ParseError("service index has no layer list")
    return {str(lyr["name"]): int(lyr["id"]) for lyr in index["layers"]}


@dataclass
class CapacityReport:
    run_id: str
    status: str = "running"
    rows_per_layer: dict[str, int] = field(default_factory=dict)
    entities_per_layer: dict[str, int] = field(default_factory=dict)
    dso_areas: int = 0
    facts_per_layer: dict[str, int] = field(default_factory=dict)
    no_zone: list[str] = field(default_factory=list)
    no_municipality: list[str] = field(default_factory=list)
    dso_ambiguous: list[str] = field(default_factory=list)
    dso_none: list[str] = field(default_factory=list)
    quirks: list[Quirk] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    id_collisions: list[str] = field(default_factory=list)
    curated_clashes: list[str] = field(default_factory=list)
    unlisted: int = 0
    raw_uris: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def facts_written(self) -> int:
        return sum(self.facts_per_layer.values())

    def text(self) -> str:
        lines = [f"capacity map run {self.run_id}: {self.status}"]
        lines += [
            f"  {layer:<32} rows {n:>5}  stations {self.entities_per_layer.get(layer, 0):>5}  "
            f"facts written {self.facts_per_layer.get(layer, 0):>6}"
            for layer, n in self.rows_per_layer.items()
        ]
        lines.append(f"  DSO licence areas {self.dso_areas}; facts written {self.facts_written}")
        lines.append(f"  entities no longer listed: {self.unlisted}")
        for label, ids in (
            ("no zone", self.no_zone),
            ("no municipality", self.no_municipality),
            ("DSO ambiguous (overlapping licence areas)", self.dso_ambiguous),
            ("no DSO licence area", self.dso_none),
            ("value conflicts (skipped)", self.conflicts),
            ("different source codes on one entity", self.id_collisions),
            ("entity IDs taken by curated entities (skipped)", self.curated_clashes),
        ):
            if ids:
                more = " ..." if len(ids) > 8 else ""
                lines.append(f"  {label}: {len(ids)}: {', '.join(ids[:8])}{more}")
        if self.quirks:
            by_pair: dict[str, int] = {}
            for q in self.quirks:
                by_pair[q.lower_field] = by_pair.get(q.lower_field, 0) + 1
            lines.append(f"  lower bound above upper bound (kept as published): {len(self.quirks)} rows")
            lines += [f"    {f}: {n}" for f, n in by_pair.items()]
        if self.error:
            lines.append(f"  error: {self.error}")
        return "\n".join(lines)


def spatial_join(
    conn: psycopg.Connection[Any], points: Sequence[tuple[float, float]]
) -> dict[tuple[float, float], Location]:
    """Zones, municipalities and DSO licence areas whose polygon covers each point."""
    if not points:
        return {}

    def covering(kind: str) -> str:
        return (
            "array(select g.entity_id from ref.entity_geometry g join ref.entity e on e.entity_id = g.entity_id "
            f"where e.kind = '{kind}' and e.country_code = %s and st_covers(g.geom, p.geom) order by g.entity_id)"
        )

    rows = conn.execute(
        f"""
        with p as (
            select k, st_setsrid(st_makepoint(x, y), 4326) as geom
            from unnest(%s::int[], %s::float8[], %s::float8[]) as t(k, x, y)
        )
        select p.k, {covering("zone")}, {covering("municipality")}, {covering("dso_area")}
        from p
        """,
        (
            list(range(len(points))),
            [x for x, _ in points],
            [y for _, y in points],
            COUNTRY,
            COUNTRY,
            COUNTRY,
        ),
    ).fetchall()
    return {points[k]: Location(tuple(z), tuple(m), tuple(d)) for k, z, m, d in rows}


def _upsert_entity(
    conn: psycopg.Connection[Any], eid: str, kind: str, parent: str | None, name: str, attributes: dict[str, Any]
) -> None:
    conn.execute(
        """
        insert into ref.entity (entity_id, kind, country_code, parent_id, name, attributes, sensitivity, source_path)
        values (%s, %s, %s, %s, %s, %s, 'public', %s)
        on conflict (entity_id) do update set kind = excluded.kind, parent_id = excluded.parent_id,
            name = excluded.name, attributes = excluded.attributes, source_path = excluded.source_path,
            loaded_at = now()
        """,
        (eid, kind, COUNTRY, parent, name, Jsonb(attributes), SOURCE_PATH),
    )


def _upsert_geometry(
    conn: psycopg.Connection[Any], eid: str, geom_sql: str, params: tuple[Any, ...], source: str
) -> None:
    conn.execute(
        f"""
        insert into ref.entity_geometry (entity_id, geom, source, sensitivity)
        values (%s, {geom_sql}, %s, 'public')
        on conflict (entity_id) do update set geom = excluded.geom, source = excluded.source, loaded_at = now()
        """,
        (eid, *params, source),
    )


def _curated_ids(conn: psycopg.Connection[Any], ids: Sequence[str]) -> set[str]:
    rows = conn.execute(
        "select entity_id from ref.entity where entity_id = any(%s) and source_path is distinct from %s",
        (list(ids), SOURCE_PATH),
    ).fetchall()
    return {r[0] for r in rows}


def _start(conn: psycopg.Connection[Any], run_id: uuid.UUID) -> None:
    with conn.transaction():
        conn.execute(
            "insert into ops.ingest_run (run_id, source, dataset, status, code_version) "
            "values (%s, %s, %s, 'running', %s)",
            (run_id, SOURCE, DATASET, code_version()),
        )


def _finish(conn: psycopg.Connection[Any], report: CapacityReport, rows_received: int) -> None:
    with conn.transaction():
        conn.execute(
            """
            update ops.ingest_run set finished_at = clock_timestamp(), status = %s, rows_received = %s,
                rows_written = %s, raw_uris = %s, error = %s
            where run_id = %s
            """,
            (
                report.status,
                rows_received,
                report.facts_written,
                report.raw_uris,
                report.error,
                uuid.UUID(report.run_id),
            ),
        )


def _bead(conn: psycopg.Connection[Any], report: CapacityReport) -> None:
    """One open human bead for stations without a unique zone, municipality or DSO (deduplicated by title)."""
    if not (report.no_zone or report.no_municipality or report.dso_ambiguous or report.dso_none):
        return
    title = "capacity map: stations without a unique zone, municipality or DSO licence area"
    body = (
        f"Run {report.run_id} of the capacity-map loader left these attributes null. "
        f"No zone: {report.no_zone[:20]}. No municipality: {report.no_municipality[:20]}. "
        f"Several overlapping DSO licence areas (attributes.dso_candidates): {len(report.dso_ambiguous)}, "
        f"e.g. {report.dso_ambiguous[:10]}. No DSO licence area: {report.dso_none[:20]}. "
        "Decide the rule (for example the smaller, enclosed licence area) or approve per-station assignments."
    )
    conn.execute(
        """
        insert into ops.bead_outbox (kind, title, body, source, dataset)
        select 'human', %s, %s, %s, %s
        where not exists (select 1 from ops.bead_outbox where title = %s and drained_at is null)
        """,
        (title, body, SOURCE, DATASET, title),
    )


def load_capacity_map(
    conn: psycopg.Connection[Any],
    *,
    fetch: Fetch | None = None,
    store: RawStore | None = None,
    layers: Sequence[Layer] = LAYERS,
) -> CapacityReport:
    """Run the loader once (see the module docstring)."""
    run_id = uuid.uuid4()
    report = CapacityReport(str(run_id))
    cap = _Captures(fetch or http_fetch(), store or RawStore(get_settings().raw_store_url), run_id)
    _start(conn, run_id)
    rows: list[StationRow] = []
    try:
        index, _ = cap.json(f"{SERVICE_URL}", {"f": "json"}, "service_index")
        boundary_index, _ = cap.json(f"{BOUNDARY_SERVICE_URL}", {"f": "json"}, f"{BOUNDARY_LAYER}_service_index")
        areas = [
            a for fc, _ in cap.layer(BOUNDARY_SERVICE_URL, boundary_index, BOUNDARY_LAYER) for a in parse_dso_areas(fc)
        ]
        for layer in layers:
            for fc, doc_id in cap.layer(SERVICE_URL, index, layer.name):
                rows += parse_layer(layer, fc, doc_id)
        with conn.transaction():
            _write(conn, cap, areas, rows, report)
        report.status = "ok"
    except Exception as exc:
        report.status, report.error = "failed", f"{type(exc).__name__}: {exc}"[:2000]
        raise
    finally:
        report.raw_uris = [d["raw_uri"] for d in cap.documents]
        _finish(conn, report, len(rows))
    return report


def _write(
    conn: psycopg.Connection[Any],
    cap: _Captures,
    areas: Sequence[Any],
    rows: Sequence[StationRow],
    report: CapacityReport,
) -> None:
    for d in cap.documents:
        db.record_source_document(conn, source=SOURCE, **d)
    clashes = _curated_ids(conn, [a.entity_id for a in areas])
    for a in areas:
        if a.entity_id in clashes:
            continue
        _upsert_entity(conn, a.entity_id, "dso_area", country_entity_id(COUNTRY), a.name, a.attributes)
        _upsert_geometry(
            conn,
            a.entity_id,
            "st_setsrid(st_geomfromgeojson(%s), 4326)",
            (json.dumps(a.geometry),),
            f"{SOURCE}:{BOUNDARY_LAYER}",
        )
    report.dso_areas = len(areas) - len(clashes)

    locations = spatial_join(conn, sorted({r.point for r in rows}))
    stations = build_stations(rows, locations)
    report.rows_per_layer, report.entities_per_layer = stations.rows_per_layer, stations.entities_per_layer
    report.quirks, report.conflicts, report.id_collisions = stations.quirks, stations.conflicts, stations.id_collisions
    clashes |= _curated_ids(conn, list(stations.entities))
    report.curated_clashes = sorted(clashes)
    for e in stations.entities.values():
        if e.entity_id in clashes:
            continue
        a = e.attributes
        if a["zone"] is None:
            report.no_zone.append(e.entity_id)
        if a["municipality"] is None:
            report.no_municipality.append(e.entity_id)
        if "dso" in a and a["dso"] is None:
            (report.dso_ambiguous if a.get("dso_candidates") else report.dso_none).append(e.entity_id)
        _upsert_entity(conn, e.entity_id, "substation", e.parent_id, e.name, a)
        _upsert_geometry(conn, e.entity_id, "st_setsrid(st_makepoint(%s, %s), 4326)", (e.lon, e.lat), e.geometry_source)

    seen = [a.entity_id for a in areas] + list(stations.entities)
    report.unlisted = conn.execute(
        """
        update ref.entity set attributes = attributes || '{"listed": false}', loaded_at = now()
        where source_path = %s and not (entity_id = any(%s)) and coalesce((attributes->>'listed')::boolean, true)
        """,
        (SOURCE_PATH, seen),
    ).rowcount

    by_layer: dict[str, list[db.FactRow]] = {}
    layer_of = {f.entry_id: lyr.name for lyr in LAYERS for f in lyr.facts}
    for c in stations.candidates:
        if c.entity_id in clashes:
            continue
        by_layer.setdefault(layer_of[c.entry_id], []).append(
            db.FactRow(
                entity_id=c.entity_id,
                attribute=c.entry_id,
                value=c.value,
                unit=c.unit,
                valid_from=c.valid_from,
                known_at=cap.fetched_at(c.document_id),
                source_document_id=c.document_id,
                run_id=uuid.UUID(report.run_id),
            )
        )
    for layer_name, facts in by_layer.items():
        report.facts_per_layer[layer_name] = db.write_facts(conn, facts)
    _bead(conn, report)
