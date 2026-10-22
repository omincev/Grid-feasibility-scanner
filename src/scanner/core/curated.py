"""Curated-file loader (WP0.4, ADR 0006): curated files in git -> ref and core tables.

Every file is parsed with the scanner.core models and checked (contract checks, unique keys,
declared references) before anything is written; any error aborts the load. Folders then load in
foreign-key order (FOLDERS), one transaction per folder, as role scanner_curator.

Idempotence: ref.curated_file records each file's SHA-256 and the natural keys it produced.
Unchanged files are skipped; new and changed files are upserted. Keys that no file declares any more
are deleted when nothing references them (a foreign-key violation keeps the row) and are reported
either way. ref.market_rule has no natural key, so a changed file replaces all of that file's rows.

Read from the repository root, then the same layout under Settings.restricted_curated_path with
source paths prefixed "restricted:":

- registry/countries/<cc>.yaml -> ref.country, entity <cc>.country.<cc>, one zone entity per zone
- codebook/classes.yaml, codebook/<cc>/*.yaml -> ref.equivalence_class, ref.codebook_entry
- sources named by manifests or codebook files, with their docs/sources dossier -> ref.source
- src/scanner/adapters/*/manifests/*.yaml and registry/snapshots.yaml -> ref.dataset (freshness metadata)
- registry/entities/<cc>/entities.yaml and aliases.yaml -> ref.entity, ref.entity_alias
- registry/geometries/**/<name>.geojson with <name>.meta.yaml -> ref.entity_geometry
- market_rules/*.yaml -> ref.market_rule
- assumptions/**/*.yaml except assumptions/scenarios/ -> ref.assumption_set
- assumptions/scenarios/*.yaml -> ref.scenario
- docs/methods/*.md (method cards, front matter and body) -> ref.method_card
- benchmarks/*.yaml -> ref.benchmark
- claims/<cc>/*.yaml -> core.source_document, core.claim (claims/README.md)
"""

from __future__ import annotations

import hashlib
import itertools
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import psycopg
import yaml
from psycopg import errors, sql
from psycopg.types.json import Jsonb
from pydantic_core import to_jsonable_python

from scanner.core.codebook import CodebookEntry, CodebookFile, EquivalenceClass
from scanner.core.contracts import run_all
from scanner.core.manifest import Manifest, load_manifests, manifest_paths
from scanner.core.methods import card_paths, parse_card
from scanner.core.registry import (
    SCENARIOS_DIR,
    AliasesFile,
    AssumptionSet,
    Benchmark,
    ClaimsFile,
    Country,
    EntitiesFile,
    GeometryMeta,
    MarketRulesFile,
    Scenario,
    Snapshot,
    assumption_paths,
    country_entity_id,
)
from scanner.core.sensitivity import Sensitivity, combine

T = TypeVar("T")
Row = dict[str, Any]
Key = tuple[str, ...]

RESTRICTED_PREFIX = "restricted:"
SOURCES_PATH = "derived:ref.source"
CURATOR_ROLE = "scanner_curator"


@dataclass(frozen=True)
class Table:
    name: str
    key: tuple[str, ...]  # natural key; empty means a changed file replaces its rows (by source_path)
    loaded_at: bool = False  # has a loaded_at column to refresh on update
    geometry: str | None = None  # column that receives a GeoJSON geometry


TABLES: dict[str, Table] = {
    t.name: t
    for t in (
        Table("ref.country", ("code",), loaded_at=True),
        Table("ref.entity", ("entity_id",), loaded_at=True),
        Table("ref.equivalence_class", ("class",)),
        Table("ref.codebook_entry", ("entry_id",)),
        Table("ref.source", ("source",)),
        Table("ref.dataset", ("source", "dataset"), loaded_at=True),
        Table("ref.entity_alias", ("source", "source_name")),
        Table("ref.entity_geometry", ("entity_id",), loaded_at=True, geometry="geom"),
        Table("ref.market_rule", ()),
        Table("ref.assumption_set", ("set_id",), loaded_at=True),
        Table("ref.scenario", ("scenario_id",), loaded_at=True),
        Table("ref.method_card", ("method_id",), loaded_at=True),
        Table("ref.benchmark", ("benchmark_id",)),
        Table("core.source_document", ("document_id",)),
        Table("core.claim", ("claim_id",)),
    )
}

# Load order: folders in this order, tables within a folder in the listed order (parents first).
FOLDERS: dict[str, tuple[str, ...]] = {
    "countries": ("ref.country", "ref.entity"),
    "codebook": ("ref.equivalence_class", "ref.codebook_entry"),
    "sources": ("ref.source",),
    "datasets": ("ref.dataset",),
    "entities": ("ref.entity", "ref.entity_alias"),
    "geometries": ("ref.entity_geometry",),
    "market_rules": ("ref.market_rule",),
    "assumptions": ("ref.assumption_set",),
    "scenarios": ("ref.scenario",),
    "methods": ("ref.method_card",),
    "benchmarks": ("ref.benchmark",),
    "claims": ("core.source_document", "core.claim"),
}

# The foreign keys of the target tables, checked against curated files before writing.
REFERENCES: tuple[tuple[str, str, str], ...] = (
    ("ref.entity", "country_code", "ref.country"),
    ("ref.entity", "parent_id", "ref.entity"),
    ("ref.entity_alias", "entity_id", "ref.entity"),
    ("ref.entity_geometry", "entity_id", "ref.entity"),
    ("ref.codebook_entry", "country_code", "ref.country"),
    ("ref.codebook_entry", "equivalence_class", "ref.equivalence_class"),
    ("ref.market_rule", "country_code", "ref.country"),
    ("ref.market_rule", "zone_id", "ref.entity"),
    ("ref.benchmark", "entity_id", "ref.entity"),
    ("ref.benchmark", "zone_id", "ref.entity"),
    ("core.claim", "document_id", "core.source_document"),
    ("core.claim", "entity_id", "ref.entity"),
)

# Curated material above public lives in the restricted repository (ADR 0006, 0008).
PUBLIC_ONLY = (
    "ref.entity",
    "ref.entity_geometry",
    "ref.assumption_set",
    "ref.scenario",
    "core.source_document",
    "core.claim",
)

GEOMETRY_TYPES = {"Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon"}


class CuratedError(ValueError):
    """Curated files failed validation; nothing was written."""

    def __init__(self, problems: list[str]):
        super().__init__(f"{len(problems)} curated-file error(s):\n" + "\n".join(f"  - {p}" for p in problems))
        self.problems = problems


def _key(table: Table, row: Row) -> Key:
    return tuple(str(row[c]) for c in table.key)


def _label(table: str, key: Key) -> str:
    return f"{table} {'/'.join(key)}"


@dataclass(frozen=True)
class Unit:
    """One curated file (or the derived source list) and the rows it produces, per table."""

    folder: str
    path: str  # stored as source_path; relative to its root, restricted files prefixed
    content_hash: str
    rows: dict[str, list[Row]]

    def keys(self) -> dict[str, list[Key]]:
        return {t: [_key(TABLES[t], r) for r in rows] for t, rows in self.rows.items() if TABLES[t].key}

    @property
    def sensitivity(self) -> Sensitivity:
        return combine(*(r["sensitivity"] for rows in self.rows.values() for r in rows if "sensitivity" in r))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _jsonb(value: Any) -> Jsonb:
    """A jsonb value; dates and other YAML scalars take their JSON form."""
    return Jsonb(to_jsonable_python(value))


def _entity_row(
    entity_id: str,
    kind: str,
    country: str,
    parent: str | None,
    name: str,
    source_path: str,
    *,
    eic: str | None = None,
    attributes: dict[str, Any] | None = None,
    sensitivity: Sensitivity = "public",
) -> Row:
    return {
        "entity_id": entity_id,
        "kind": kind,
        "country_code": country,
        "parent_id": parent,
        "name": name,
        "eic": eic,
        "attributes": _jsonb(attributes or {}),
        "sensitivity": sensitivity,
        "source_path": source_path,
    }


def _codebook_row(f: CodebookFile, e: CodebookEntry, path: str, digest: str) -> Row:
    return {
        "entry_id": e.id,
        "source": f.source,
        "dataset": f.dataset,
        "country_code": f.country,
        "field": e.field,
        "dimensions": _jsonb(e.dimensions),
        "equivalence_class": e.equivalence_class,
        "kind": e.kind,
        "unit": e.unit,
        "sign_convention": e.sign_convention,
        "resample": e.resample,
        "issued_at_rule": None if e.issued_at is None else _jsonb(e.issued_at.model_dump(mode="json")),
        "definition": _jsonb(e.definition.model_dump(mode="json")),
        "notes": e.notes,
        "status": f.status,
        "verified_by": f.verified_by,
        "verified_on": f.verified_on,
        "sensitivity": e.sensitivity,
        "source_path": path,
        "content_hash": digest,
    }


def _manifest_dataset_row(m: Manifest, path: str) -> Row:
    return {
        "source": m.source,
        "dataset": m.dataset,
        "kind": "manifest",
        "country_code": m.country,
        "status": m.status,
        "schedule": list(m.schedule),
        "schedule_timezone": m.schedule_timezone,
        "freshness_sla": m.freshness_sla,
        "resolution": m.resolution,
        "history_start": m.history_start_local,
        "history_end": m.history_end_local,
        "licence": m.licence,
        "owner": None,
        "sensitivity": m.sensitivity,
        "source_path": path,
    }


def _snapshot_dataset_row(s: Snapshot, path: str) -> Row:
    """Snapshots are datasets of source 'snapshots', as in the raw store and ops.snapshot; they have no SLA."""
    return {
        "source": "snapshots",
        "dataset": s.id,
        "kind": "snapshot",
        "country_code": None,
        "status": s.status,
        "schedule": [s.schedule],
        "schedule_timezone": s.timezone,
        "freshness_sla": None,
        "resolution": None,
        "history_start": None,
        "history_end": None,
        "licence": None,
        "owner": s.owner,
        "sensitivity": "public",
        "source_path": path,
    }


def _manifest(raw: Any) -> Manifest:
    return Manifest(**raw)


def _snapshots(raw: Any) -> list[Snapshot]:
    return [Snapshot(**s) for s in raw["snapshots"]]


def _classes(raw: Any) -> dict[str, EquivalenceClass]:
    return {k: EquivalenceClass.model_validate(v) for k, v in raw["classes"].items()}


def _benchmarks(raw: Any) -> list[Benchmark]:
    return [Benchmark.model_validate(b) for b in raw["benchmarks"]]


def _positions(coords: Any) -> Iterator[list[float]]:
    if coords and isinstance(coords[0], int | float):
        yield coords
    else:
        for c in coords:
            yield from _positions(c)


def geojson_features(fc: Any) -> list[tuple[str, dict[str, Any]]]:
    """(entity_id, geometry) for each feature of an EPSG:4326 FeatureCollection; ValueError if invalid."""
    if not isinstance(fc, dict) or fc.get("type") != "FeatureCollection":
        raise ValueError("not a GeoJSON FeatureCollection")
    crs = str(((fc.get("crs") or {}).get("properties") or {}).get("name", "CRS84"))
    if not crs.endswith(("CRS84", "4326")):
        raise ValueError(f"crs {crs} is not EPSG:4326")
    out: list[tuple[str, dict[str, Any]]] = []
    for i, feature in enumerate(fc["features"]):
        entity_id = (feature.get("properties") or {}).get("entity_id")
        geometry = feature.get("geometry") or {}
        if not isinstance(entity_id, str):
            raise ValueError(f"feature {i}: properties.entity_id is missing")
        if geometry.get("type") not in GEOMETRY_TYPES:
            raise ValueError(f"feature {i} ({entity_id}): unsupported geometry type {geometry.get('type')}")
        for lon, lat, *_ in _positions(geometry["coordinates"]):
            if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                raise ValueError(f"feature {i} ({entity_id}): position ({lon}, {lat}) is outside EPSG:4326")
        out.append((entity_id, geometry))
    return out


def _dossier_name(path: Path) -> str | None:
    first = path.read_text(encoding="utf-8").partition("\n")[0]
    prefix = "# Source dossier:"
    return first.removeprefix(prefix).strip() if first.startswith(prefix) else None


class _Reader:
    """Parses one root's curated folders into units, collecting errors instead of raising."""

    def __init__(self, root: Path, prefix: str) -> None:
        self.root, self.prefix = root, prefix
        self.errors: list[str] = []

    def rel(self, path: Path) -> str:
        return self.prefix + path.relative_to(self.root).as_posix()

    def parse(self, path: Path, build: Callable[[Any], T]) -> tuple[T, str, str] | None:
        """(object, stored path, SHA-256) for a YAML file, or None with the error recorded."""
        data = path.read_bytes()
        try:
            return build(yaml.safe_load(data)), self.rel(path), _sha256(data)
        except (yaml.YAMLError, ValueError, TypeError, KeyError, AttributeError) as exc:
            self.errors.append(f"{self.rel(path)}: {exc}")
            return None

    def read(self) -> list[Unit]:
        return list(
            itertools.chain(
                self.countries(),
                self.codebook(),
                self.datasets(),
                self.entities(),
                self.geometries(),
                self.market_rules(),
                self.assumptions(),
                self.scenarios(),
                self.methods(),
                self.benchmarks(),
                self.claims(),
            )
        )

    def countries(self) -> Iterator[Unit]:
        for path in sorted((self.root / "registry" / "countries").glob("*.yaml")):
            if (parsed := self.parse(path, Country.model_validate)) is None:
                continue
            c, rel, digest = parsed
            if path.stem != c.code:
                self.errors.append(f"{rel}: file name must equal country code {c.code}")
            cid = country_entity_id(c.code)
            entities = [_entity_row(cid, "country", c.code, None, c.name, rel)]
            entities += [
                _entity_row(
                    z.id,
                    "zone",
                    c.code,
                    cid,
                    z.name,
                    rel,
                    eic=z.eic,
                    attributes={"short": z.short, "eic_verified": z.eic_verified},
                )
                for z in c.zones
            ]
            country = {
                "code": c.code,
                "name": c.name,
                "status": c.status,
                "timezone": c.timezone,
                "currency": c.currency,
                "tso": c.tso,
                "config": _jsonb(c.model_dump(mode="json")),
                "source_path": rel,
                "content_hash": digest,
            }
            yield Unit("countries", rel, digest, {"ref.country": [country], "ref.entity": entities})

    def codebook(self) -> Iterator[Unit]:
        classes = self.root / "codebook" / "classes.yaml"
        if classes.exists() and (parsed := self.parse(classes, _classes)) is not None:
            found, rel, digest = parsed
            rows = [{"class": k, "dimension": v.dimension, "description": v.description} for k, v in found.items()]
            yield Unit("codebook", rel, digest, {"ref.equivalence_class": rows})
        for path in sorted((self.root / "codebook").glob("*/*.yaml")):
            if (parsed_file := self.parse(path, CodebookFile.model_validate)) is None:
                continue
            f, rel, digest = parsed_file
            entries = [_codebook_row(f, e, rel, digest) for e in f.entries]
            yield Unit("codebook", rel, digest, {"ref.codebook_entry": entries})

    def datasets(self) -> Iterator[Unit]:
        for path in manifest_paths(self.root):
            if (parsed := self.parse(path, _manifest)) is not None:
                m, rel, digest = parsed
                yield Unit("datasets", rel, digest, {"ref.dataset": [_manifest_dataset_row(m, rel)]})
        path = self.root / "registry" / "snapshots.yaml"
        if path.exists() and (snaps := self.parse(path, _snapshots)) is not None:
            found, rel, digest = snaps
            yield Unit("datasets", rel, digest, {"ref.dataset": [_snapshot_dataset_row(s, rel) for s in found]})

    def entities(self) -> Iterator[Unit]:
        base = self.root / "registry" / "entities"
        for path in sorted(base.glob("*/entities.yaml")):
            if (parsed := self.parse(path, EntitiesFile.model_validate)) is None:
                continue
            f, rel, digest = parsed
            cc = path.parent.name
            rows = []
            for e in f.entities:
                if not e.id.startswith(f"{cc}."):
                    self.errors.append(f"{rel}: {e.id} does not belong to country folder {cc}")
                rows.append(
                    _entity_row(
                        e.id,
                        e.kind,
                        cc,
                        e.parent,
                        e.name,
                        rel,
                        eic=e.eic,
                        attributes=e.attributes,
                        sensitivity=e.sensitivity,
                    )
                )
            yield Unit("entities", rel, digest, {"ref.entity": rows})
        for path in sorted(base.glob("*/aliases.yaml")):
            if (parsed_aliases := self.parse(path, AliasesFile.model_validate)) is None:
                continue
            a_file, rel, digest = parsed_aliases
            aliases = [
                {
                    "entity_id": a.entity_id,
                    "source": a.source,
                    "source_id": a.source_id,
                    "source_name": a.source_name,
                    "approved_by": a.approved_by,
                    "approved_on": a.approved_on,
                    "evidence": None if a.evidence is None else _jsonb(a.evidence),
                }
                for a in a_file.aliases
            ]
            yield Unit("entities", rel, digest, {"ref.entity_alias": aliases})

    def geometries(self) -> Iterator[Unit]:
        for path in sorted((self.root / "registry" / "geometries").rglob("*.geojson")):
            rel = self.rel(path)
            meta_path = path.with_suffix(".meta.yaml")
            if not meta_path.exists():
                self.errors.append(f"{rel}: missing sibling {meta_path.name}")
                continue
            if (parsed := self.parse(meta_path, GeometryMeta.model_validate)) is None:
                continue
            meta = parsed[0]
            data = path.read_bytes()
            try:
                features = geojson_features(json.loads(data))
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                self.errors.append(f"{rel}: {exc}")
                continue
            rows = [
                {
                    "entity_id": entity_id,
                    "geom": json.dumps(geometry),
                    "source": rel,
                    "blob_id": None,
                    "approved_by": meta.approved_by,
                    "sensitivity": meta.sensitivity,
                }
                for entity_id, geometry in features
            ]
            # The meta file is part of the unit: a changed approver or sensitivity must reload.
            yield Unit("geometries", rel, _sha256(data + b"\0" + meta_path.read_bytes()), {"ref.entity_geometry": rows})

    def market_rules(self) -> Iterator[Unit]:
        for path in sorted((self.root / "market_rules").glob("*.yaml")):
            if (parsed := self.parse(path, MarketRulesFile.model_validate)) is None:
                continue
            f, rel, digest = parsed
            rows = [
                {
                    "country_code": f.country,
                    "zone_id": r.zone,
                    "rule_key": r.key,
                    "value": _jsonb(r.value),
                    "valid_from": r.valid_from,
                    "valid_to": r.valid_to,
                    "precision": r.precision,
                    "source": r.source,
                    "verified": r.verified,
                    "note": r.note,
                    "source_path": rel,
                }
                for r in f.rules
            ]
            yield Unit("market_rules", rel, digest, {"ref.market_rule": rows})

    def assumptions(self) -> Iterator[Unit]:
        for path in assumption_paths(self.root):
            if (parsed := self.parse(path, AssumptionSet.model_validate)) is None:
                continue
            a, rel, digest = parsed
            if path.stem != a.id:
                self.errors.append(f"{rel}: file name must equal set id {a.id}")
            row = {
                "set_id": a.id,
                "kind": a.kind,
                "status": a.status,
                "owners": a.owners,
                "approved_by": a.approved_by,
                "approved_on": a.approved_on,
                "parameters": _jsonb(a.model_dump(mode="json")["parameters"]),
                "sensitivity": a.sensitivity,
                "source_path": rel,
                "content_hash": digest,
            }
            yield Unit("assumptions", rel, digest, {"ref.assumption_set": [row]})

    def scenarios(self) -> Iterator[Unit]:
        for path in sorted((self.root / "assumptions" / SCENARIOS_DIR).glob("*.yaml")):
            if (parsed := self.parse(path, Scenario.model_validate)) is None:
                continue
            s, rel, digest = parsed
            if path.stem != s.id:
                self.errors.append(f"{rel}: file name must equal scenario id {s.id}")
            row = {
                "scenario_id": s.id,
                "title": s.title,
                "status": s.status,
                "description": s.description,
                "owners": s.owners,
                "approved_by": s.approved_by,
                "approved_on": s.approved_on,
                "sets": _jsonb(s.sets),
                "method_switches": _jsonb(s.method_switches),
                "overrides": _jsonb(s.overrides),
                "sensitivity": s.sensitivity,
                "source_path": rel,
                "content_hash": digest,
            }
            yield Unit("scenarios", rel, digest, {"ref.scenario": [row]})

    def methods(self) -> Iterator[Unit]:
        for path in card_paths(self.root):
            rel, data = self.rel(path), path.read_bytes()
            try:
                card = parse_card(data.decode("utf-8"))
            except (yaml.YAMLError, ValueError, TypeError) as exc:
                self.errors.append(f"{rel}: {exc}")
                continue
            if path.stem != card.id:
                self.errors.append(f"{rel}: file name must equal method id {card.id}")
            row = {
                "method_id": card.id,
                "version": card.version,
                "title": card.title,
                "status": card.status,
                "owner": card.owner,
                "inputs": card.inputs,
                "parameters": card.parameters,
                "outputs": card.outputs,
                "known_biases": card.known_biases,
                "body": card.body,
                "sensitivity": "public",
                "source_path": rel,
                "content_hash": _sha256(data),
            }
            yield Unit("methods", rel, _sha256(data), {"ref.method_card": [row]})

    def benchmarks(self) -> Iterator[Unit]:
        for path in sorted((self.root / "benchmarks").glob("*.yaml")):
            if (parsed := self.parse(path, _benchmarks)) is None:
                continue
            found, rel, digest = parsed
            rows = [
                {
                    "benchmark_id": b.id,
                    "entity_id": b.entity,
                    "zone_id": b.zone,
                    "description": b.description,
                    "expected": _jsonb(b.expected),
                    "owner": b.owner,
                    "source_path": rel,
                }
                for b in found
            ]
            yield Unit("benchmarks", rel, digest, {"ref.benchmark": rows})

    def claims(self) -> Iterator[Unit]:
        for path in sorted((self.root / "claims").glob("*/*.yaml")):
            if (parsed := self.parse(path, ClaimsFile.model_validate)) is None:
                continue
            f, rel, digest = parsed
            d = f.document
            document = {
                "document_id": d.id,
                "source": d.source,
                "url": d.url,
                "title": d.title,
                "fetched_at": d.fetched_at,
                "sha256": d.sha256,
                "raw_uri": d.raw_uri,
                "content_type": d.content_type,
                "sensitivity": d.sensitivity,
            }
            claims = [
                {
                    "claim_id": c.id,
                    "document_id": d.id,
                    "attribute": c.attribute,
                    "entity_id": c.entity,
                    "value_num": None if isinstance(c.value, str) else c.value,
                    "value_text": c.value if isinstance(c.value, str) else None,
                    "unit": c.unit,
                    "valid_from": c.valid_from,
                    "valid_to": c.valid_to,
                    "quote": c.quote,
                    "page": c.page,
                    "location": None if c.location is None else _jsonb(c.location),
                    "method": c.method,
                    "model": c.model,
                    "prompt_hash": c.prompt_hash,
                    "status": c.status,
                    "reviewed_by": c.reviewed_by,
                    "reviewed_on": c.reviewed_on,
                    "sensitivity": combine(c.sensitivity, d.sensitivity),  # ADR 0008
                }
                for c in f.claims
            ]
            yield Unit("claims", rel, digest, {"core.source_document": [document], "core.claim": claims})


def sources_unit(root: Path, units: list[Unit]) -> Unit:
    """ref.source: every source named by a manifest or codebook file, with its dossier when one exists.

    The licence is the manifests' licence when they agree, else null.
    """
    licences: dict[str, set[str]] = {}
    for m in load_manifests(root):
        licences.setdefault(m.source, set()).add(m.licence)
    for u in units:
        for r in u.rows.get("ref.codebook_entry", []):
            licences.setdefault(r["source"], set())
    rows = []
    for source in sorted(licences):
        dossier = next(iter(sorted((root / "docs" / "sources").glob(f"*/{source}.md"))), None)
        rows.append(
            {
                "source": source,
                "name": (_dossier_name(dossier) if dossier else None) or source,
                "dossier_path": dossier.relative_to(root).as_posix() if dossier else None,
                "licence": next(iter(licences[source])) if len(licences[source]) == 1 else None,
                "terms_url": None,
            }
        )
    digest = _sha256(json.dumps(rows, sort_keys=True).encode())
    return Unit("sources", SOURCES_PATH, digest, {"ref.source": rows})


def validate(units: list[Unit]) -> list[str]:
    """Cross-file checks: unique keys and EIC codes, declared references, no cycles, public main repo."""
    problems: list[str] = []
    owner: dict[tuple[str, Key], str] = {}
    for u in units:
        for table, keys in u.keys().items():
            for k in keys:
                if (table, k) in owner:
                    problems.append(f"{u.path}: {_label(table, k)} is also declared in {owner[table, k]}")
                owner.setdefault((table, k), u.path)
    declared = {(table, k[0]) for table, k in owner if len(k) == 1}
    for table, column, target in REFERENCES:
        for u in units:
            for r in u.rows.get(table, []):
                if r[column] is not None and (target, r[column]) not in declared:
                    problems.append(f"{u.path}: {table}.{column} {r[column]} is not declared in any curated file")

    entities = {r["entity_id"]: (u.path, r) for u in units for r in u.rows.get("ref.entity", [])}
    eics: dict[str, str] = {}
    for entity_id, (path, r) in entities.items():
        if r["eic"] is not None:
            if r["eic"] in eics:
                problems.append(f"{path}: EIC {r['eic']} of {entity_id} is also used by {eics[r['eic']]}")
            eics.setdefault(r["eic"], entity_id)
        seen, parent = {entity_id}, r["parent_id"]
        while parent in entities:
            if parent in seen:
                problems.append(f"{path}: {entity_id} is its own ancestor")
                break
            seen.add(parent)
            parent = entities[parent][1]["parent_id"]

    for u in units:
        if u.path.startswith(RESTRICTED_PREFIX):
            continue
        for table in PUBLIC_ONLY:
            for r in u.rows.get(table, []):
                if r["sensitivity"] != "public":
                    problems.append(
                        f"{u.path}: {table} row is {r['sensitivity']}; "
                        "non-public curated data belongs in the restricted repository (ADR 0006)"
                    )
    return problems


def read_curated(root: Path, restricted_root: Path | None = None) -> list[Unit]:
    """Parse and validate every curated file; raise CuratedError listing every problem found."""
    problems = [f"contract {name}: {e}" for name, errs in run_all(root).items() for e in errs]
    readers = [_Reader(root, "")]
    if restricted_root is not None:
        # A mistyped path would otherwise read as "every restricted file was removed".
        if not restricted_root.is_dir():
            problems.append(f"restricted curated path {restricted_root} is not a directory")
        readers.append(_Reader(restricted_root, RESTRICTED_PREFIX))
    units = [u for reader in readers for u in reader.read()]
    problems += [e for reader in readers for e in reader.errors]
    if problems:
        raise CuratedError(problems)
    units.append(sources_unit(root, units))
    problems = validate(units)
    if problems:
        raise CuratedError(problems)
    return units


@dataclass
class FolderChange:
    """What a load does (or in a dry run would do) to one folder."""

    folder: str
    new: list[Unit] = field(default_factory=list)
    changed: list[Unit] = field(default_factory=list)
    removed_files: list[str] = field(default_factory=list)
    unchanged: int = 0
    removed_keys: dict[str, list[Key]] = field(default_factory=dict)  # declared before, by no file now
    deleted: list[str] = field(default_factory=list)  # filled by apply
    kept: list[str] = field(default_factory=list)  # removed from files but still referenced

    @property
    def changes(self) -> int:
        return len(self.new) + len(self.changed) + len(self.removed_files)

    def upserts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for u in self.new + self.changed:
            for table, rows in u.rows.items():
                counts[table] = counts.get(table, 0) + len(rows)
        return counts


@dataclass
class Report:
    dry_run: bool
    folders: list[FolderChange]

    @property
    def changes(self) -> int:
        return sum(f.changes for f in self.folders)

    def text(self) -> str:
        would = "would " if self.dry_run else ""
        lines: list[str] = []
        for f in self.folders:
            counts = [
                f"{n} {label}"
                for n, label in (
                    (len(f.new), "new"),
                    (len(f.changed), "changed"),
                    (len(f.removed_files), "removed"),
                    (f.unchanged, "unchanged"),
                )
                if n
            ]
            lines.append(f"{f.folder}: {', '.join(counts) or 'no files'}")
            lines += [f"  new      {u.path}" for u in f.new]
            lines += [f"  changed  {u.path}" for u in f.changed]
            lines += [f"  removed  {p}" for p in f.removed_files]
            lines += [f"  {would}upsert {n} {table} row(s)" for table, n in f.upserts().items() if n]
            if self.dry_run:
                removed = [_label(t, k) for t, keys in f.removed_keys.items() for k in keys]
                lines += [f"  would delete unless referenced: {r}" for r in removed]
            else:
                lines += [f"  deleted: {r}" for r in f.deleted]
                lines += [f"  kept (still referenced, no longer in any file): {r}" for r in f.kept]
        suffix = " (dry run, nothing written)" if self.dry_run else ""
        lines.append(f"{self.changes} file change(s){suffix}")
        return "\n".join(lines)


def plan(conn: psycopg.Connection[Any], units: list[Unit], *, restricted: bool) -> list[FolderChange]:
    """Compare files with ref.curated_file. Restricted files are left alone unless restricted is set."""
    loaded: dict[str, tuple[str, str, dict[str, list[Key]]]] = {
        path: (folder, digest, {t: [tuple(k) for k in ks] for t, ks in keys.items()})
        for path, folder, digest, keys in conn.execute(
            "select source_path, folder, content_hash, keys from ref.curated_file"
        )
        if restricted or not path.startswith(RESTRICTED_PREFIX)
    }
    declared = {(t, k) for u in units for t, keys in u.keys().items() for k in keys}
    out: list[FolderChange] = []
    for folder in FOLDERS:
        fc = FolderChange(folder)
        current = [u for u in units if u.folder == folder]
        for u in current:
            if u.path not in loaded:
                fc.new.append(u)
            elif loaded[u.path][1] != u.content_hash:
                fc.changed.append(u)
            else:
                fc.unchanged += 1
        paths = {u.path for u in current}
        fc.removed_files = sorted(p for p, (f, _, _) in loaded.items() if f == folder and p not in paths)
        for p in [u.path for u in fc.changed] + fc.removed_files:
            for table, keys in loaded[p][2].items():
                fc.removed_keys.setdefault(table, []).extend(k for k in keys if (table, k) not in declared)
        out.append(fc)
    return out


def _ident(table: str) -> sql.Identifier:
    return sql.Identifier(*table.split("."))


def _upsert(conn: psycopg.Connection[Any], table: Table, rows: list[Row]) -> None:
    if not rows:
        return
    cols = list(rows[0])
    values = [
        sql.SQL("ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)") if c == table.geometry else sql.SQL("%s") for c in cols
    ]
    query = sql.SQL("insert into {} ({}) values ({})").format(
        _ident(table.name), sql.SQL(", ").join(map(sql.Identifier, cols)), sql.SQL(", ").join(values)
    )
    if table.key:
        updates: list[sql.Composable] = [
            sql.SQL("{0} = excluded.{0}").format(sql.Identifier(c)) for c in cols if c not in table.key
        ]
        if table.loaded_at:
            updates.append(sql.SQL("loaded_at = now()"))
        query += sql.SQL(" on conflict ({}) do update set {}").format(
            sql.SQL(", ").join(map(sql.Identifier, table.key)), sql.SQL(", ").join(updates)
        )
    with conn.cursor() as cur:
        cur.executemany(query, [tuple(r[c] for c in cols) for r in rows])


def _parents_first(rows: list[Row]) -> list[Row]:
    by_id = {r["entity_id"]: r for r in rows}

    def depth(r: Row) -> int:
        n = 0
        while r["parent_id"] in by_id and n <= len(by_id):
            r, n = by_id[r["parent_id"]], n + 1
        return n

    return sorted(rows, key=depth)


def _delete_removed(conn: psycopg.Connection[Any], fc: FolderChange) -> None:
    """Delete keys no file declares any more, children first; keep rows that something references."""
    pending = [(t, k) for t in reversed(FOLDERS[fc.folder]) for k in fc.removed_keys.get(t, [])]
    while pending:
        retry = []
        for t, k in pending:
            where = sql.SQL(" and ").join(sql.SQL("{} = %s").format(sql.Identifier(c)) for c in TABLES[t].key)
            try:
                with conn.transaction():
                    conn.execute(sql.SQL("delete from {} where {}").format(_ident(t), where), k)
                fc.deleted.append(_label(t, k))
            except errors.ForeignKeyViolation:
                retry.append((t, k))
        if len(retry) == len(pending):
            break
        pending = retry
    fc.kept = [_label(t, k) for t, k in pending]


def apply(conn: psycopg.Connection[Any], changes: list[FolderChange]) -> None:
    """Write planned changes, one transaction (or savepoint, inside a caller's transaction) per folder."""
    for fc in changes:
        if not fc.changes:
            continue
        with conn.transaction():
            conn.execute(sql.SQL("set local role {}").format(sql.Identifier(CURATOR_ROLE)))
            # Also new files: rows may exist from a load that ref.curated_file does not remember.
            replaced = [u.path for u in fc.new + fc.changed] + fc.removed_files
            for name in FOLDERS[fc.folder]:
                table = TABLES[name]
                if not table.key:
                    conn.execute(
                        sql.SQL("delete from {} where source_path = any(%s)").format(_ident(name)), (replaced,)
                    )
                rows = [r for u in fc.new + fc.changed for r in u.rows.get(name, [])]
                _upsert(conn, table, _parents_first(rows) if name == "ref.entity" else rows)
            _delete_removed(conn, fc)
            for u in fc.new + fc.changed:
                conn.execute(
                    "insert into ref.curated_file (source_path, folder, content_hash, keys, sensitivity) "
                    "values (%s, %s, %s, %s, %s) on conflict (source_path) do update set folder = excluded.folder, "
                    "content_hash = excluded.content_hash, keys = excluded.keys, sensitivity = excluded.sensitivity, "
                    "loaded_at = now()",
                    (u.path, u.folder, u.content_hash, Jsonb(u.keys()), u.sensitivity),
                )
            if fc.removed_files:
                conn.execute("delete from ref.curated_file where source_path = any(%s)", (fc.removed_files,))
            conn.execute("reset role")


def load_units(conn: psycopg.Connection[Any], units: list[Unit], *, restricted: bool, dry_run: bool) -> Report:
    changes = plan(conn, units, restricted=restricted)
    if not dry_run:
        apply(conn, changes)
    return Report(dry_run, changes)


def load(database_url: str, root: Path, restricted_root: Path | None = None, *, dry_run: bool = False) -> Report:
    """Validate every curated file, then load the changed ones (or only report them when dry_run)."""
    units = read_curated(root, restricted_root)
    with psycopg.connect(database_url, autocommit=True) as conn:
        return load_units(conn, units, restricted=restricted_root is not None, dry_run=dry_run)
