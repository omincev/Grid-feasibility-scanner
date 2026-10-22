"""Curated-file loader (scanner.core.curated, WP0.4). Offline tests, then DB tests marked `db`.

DB tests run inside one transaction per test that is rolled back, so they leave TEST_DATABASE_URL
as they found it. Each starts by forgetting ref.curated_file so every file plans as new.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml
from pydantic import ValidationError

from scanner import REPO_ROOT
from scanner.core.curated import FOLDERS, CuratedError, Report, Unit, geojson_features, load_units, read_curated
from scanner.core.manifest import load_manifests
from scanner.core.registry import ClaimsFile, Entity, GeometryMeta, load_snapshots

CURATED_DIRS = [
    "registry",
    "codebook",
    "market_rules",
    "assumptions",
    "benchmarks",
    "claims",
    "docs/sources",
    "docs/methods",
]

META = {
    "source_url": "https://example.org/test.geojson",
    "source": "test geometry",
    "licence": "public domain (test)",
    "retrieved": "2026-10-18",
    "sha256_original": "0" * 64,
    "crs": "EPSG:4326",
    "transformation": "none",
}
SQUARE = {"type": "Polygon", "coordinates": [[[8.0, 55.0], [9.0, 55.0], [9.0, 56.0], [8.0, 56.0], [8.0, 55.0]]]}
DOCUMENT = {
    "id": "dk.test.document",
    "source": "test_source",
    "url": "https://example.org/report.pdf",
    "title": "Test report",
    "fetched_at": "2026-10-18T08:00:00Z",
    "sha256": "a" * 64,
    "raw_uri": "file://raw/test/report.pdf",
    "content_type": "application/pdf",
}
CLAIM = {
    "id": "dk.test.document.curtailment_2025",
    "attribute": "curtailment.wind.energy_gwh",
    "entity": "dk.zone.dk1",
    "value": 640,
    "unit": "GWh",
    "valid_from": "2025-01-01",
    "valid_to": "2026-01-01",
    "quote": "640 GWh",
    "page": 3,
    "method": "manual",
    "status": "pending_review",
}


def make_root(tmp_path: Path, name: str = "repo") -> Path:
    """A copy of the repository's curated folders and manifests that tests may edit."""
    root = tmp_path / name
    for d in CURATED_DIRS:
        shutil.copytree(REPO_ROOT / d, root / d)
    for manifests in (REPO_ROOT / "src" / "scanner" / "adapters").glob("*/manifests"):
        shutil.copytree(manifests, root / manifests.relative_to(REPO_ROOT))
    return root


def edit_yaml(path: Path, change: Callable[[Any], None]) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    change(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def add_entities(root: Path, *entities: dict[str, Any]) -> None:
    def change(d: Any) -> None:
        d.setdefault("entities", []).extend({"attributes": {}, **e} for e in entities)

    edit_yaml(root / "registry" / "entities" / "dk" / "entities.yaml", change)


def write_geometry(root: Path, entity_id: str, name: str = "loader_test", geometry: Any = SQUARE) -> Path:
    folder = root / "registry" / "geometries" / "dk"
    folder.mkdir(parents=True, exist_ok=True)
    fc = {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {"entity_id": entity_id}, "geometry": geometry}],
    }
    path = folder / f"{name}.geojson"
    path.write_text(json.dumps(fc), encoding="utf-8")
    (folder / f"{name}.meta.yaml").write_text(yaml.safe_dump(META), encoding="utf-8")
    return path


def rows(units: list[Unit], table: str, prefix: str = "") -> list[dict[str, Any]]:
    return [r for u in units if u.path.startswith(prefix) for r in u.rows.get(table, [])]


def _yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def yaml_counts(root: Path) -> dict[str, int]:
    """Row counts expected per table, counted straight from the YAML files."""
    countries = [_yaml(p) for p in (root / "registry" / "countries").glob("*.yaml")]
    return {
        "ref.country": len(countries),
        "ref.entity": sum(1 + len(c["zones"]) for c in countries)
        + sum(len(_yaml(p)["entities"]) for p in (root / "registry" / "entities").glob("*/entities.yaml")),
        "ref.entity_alias": sum(
            len(_yaml(p)["aliases"]) for p in (root / "registry" / "entities").glob("*/aliases.yaml")
        ),
        "ref.equivalence_class": len(_yaml(root / "codebook" / "classes.yaml")["classes"]),
        "ref.codebook_entry": sum(len(_yaml(p)["entries"]) for p in (root / "codebook").glob("*/*.yaml")),
        "ref.market_rule": sum(len(_yaml(p)["rules"]) for p in (root / "market_rules").glob("*.yaml")),
        "ref.assumption_set": len([p for p in (root / "assumptions").rglob("*.yaml") if p.parent.name != "scenarios"]),
        "ref.scenario": len(list((root / "assumptions" / "scenarios").glob("*.yaml"))),
        "ref.method_card": len([p for p in (root / "docs" / "methods").glob("*.md") if p.name != "README.md"]),
        "ref.benchmark": sum(len(_yaml(p)["benchmarks"]) for p in (root / "benchmarks").glob("*.yaml")),
        "core.claim": sum(len(_yaml(p)["claims"]) for p in (root / "claims").glob("*/*.yaml")),
    }


# Offline


def test_repository_rows_match_yaml_counts() -> None:
    units = read_curated(REPO_ROOT)
    for table, expected in yaml_counts(REPO_ROOT).items():
        assert len(rows(units, table)) == expected, table
    entities = {r["entity_id"]: r for r in rows(units, "ref.entity")}
    assert entities["dk.zone.dk1"]["parent_id"] == "dk.country.dk"
    assert entities["dk.zone.dk1"]["eic"] == "10YDK-1--------W"
    # Reference countries are loaded because manifests map their zones.
    assert {"de.zone.de_lu", "no.zone.no2", "se.zone.se3", "se.zone.se4"} <= set(entities)
    sources = {r["source"]: r for r in rows(units, "ref.source")}
    assert sources["energidataservice"]["dossier_path"] == "docs/sources/dk/energidataservice.md"
    assert sources["energidataservice"]["name"] == "Energi Data Service (Energinet)"
    assert {u.folder for u in units} <= set(FOLDERS)


def test_datasets_come_from_manifests_and_snapshots() -> None:
    units = read_curated(REPO_ROOT)
    datasets = {(r["source"], r["dataset"]): r for r in rows(units, "ref.dataset")}
    manifests, snapshots = load_manifests(REPO_ROOT), load_snapshots(REPO_ROOT)
    assert len(datasets) == len(manifests) + len(snapshots)
    dap = datasets["energidataservice", "DayAheadPrices"]
    path = "src/scanner/adapters/energidataservice/manifests/DayAheadPrices.yaml"
    assert (dap["kind"], dap["freshness_sla"], dap["sensitivity"], dap["source_path"]) == (
        "manifest",
        "PT36H",
        "public",
        path,
    )
    snap = datasets["snapshots", snapshots[0].id]
    assert (snap["kind"], snap["freshness_sla"], snap["schedule"]) == ("snapshot", None, [snapshots[0].schedule])


def test_claims_file_model() -> None:
    f = ClaimsFile.model_validate({"document": DOCUMENT, "claims": [CLAIM]})
    assert f.claims[0].value == 640
    assert (
        ClaimsFile.model_validate({"document": DOCUMENT, "claims": [{**CLAIM, "value": "free"}]}).claims[0].value
        == "free"
    )
    for bad in (
        {**CLAIM, "method": "llm"},  # needs model and prompt_hash
        {**CLAIM, "status": "approved"},  # needs reviewed_by
        {**CLAIM, "valid_to": "2024-01-01"},
    ):
        with pytest.raises(ValidationError):
            ClaimsFile.model_validate({"document": DOCUMENT, "claims": [bad]})
    with pytest.raises(ValidationError):
        ClaimsFile.model_validate({"document": {**DOCUMENT, "fetched_at": "2026-10-18T08:00:00"}, "claims": []})
    with pytest.raises(ValidationError):
        ClaimsFile.model_validate({"document": DOCUMENT, "claims": [CLAIM, CLAIM]})


def test_entity_and_geometry_meta_models() -> None:
    ok = {"id": "dk.substation.energinet.idomlund", "kind": "substation", "parent": "dk.zone.dk1", "name": "Idomlund"}
    assert Entity.model_validate(ok).attributes == {}
    for bad in (
        {**ok, "kind": "site"},  # id segment says substation
        {**ok, "id": "dk.zone.dk9", "kind": "zone"},  # zones come from country files
        {**ok, "geometry_wkt": "POINT (8 56)"},
    ):
        with pytest.raises(ValidationError):
            Entity.model_validate(bad)
    with pytest.raises(ValidationError):
        GeometryMeta.model_validate({**META, "crs": "EPSG:25832"})


def test_geojson_features() -> None:
    fc = {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {"entity_id": "x"}, "geometry": SQUARE}],
    }
    assert geojson_features(fc) == [("x", SQUARE)]
    with pytest.raises(ValueError, match="outside EPSG:4326"):
        geojson_features(
            {
                **fc,
                "features": [
                    {**fc["features"][0], "geometry": {"type": "Point", "coordinates": [500000.0, 6200000.0]}}
                ],
            }
        )
    with pytest.raises(ValueError, match="not EPSG:4326"):
        geojson_features({**fc, "crs": {"type": "name", "properties": {"name": "EPSG:25832"}}})
    with pytest.raises(ValueError, match="entity_id"):
        geojson_features({**fc, "features": [{"type": "Feature", "properties": {}, "geometry": SQUARE}]})


def _dup_entity(root: Path) -> None:
    e = {"id": "dk.site.dup", "kind": "site", "parent": "dk.zone.dk1", "name": "Dup"}
    add_entities(root, e, e)


def _unknown_parent(root: Path) -> None:
    add_entities(root, {"id": "dk.site.orphan", "kind": "site", "parent": "dk.zone.dk9", "name": "Orphan"})


def _undeclared_geometry(root: Path) -> None:
    write_geometry(root, "dk.site.nowhere")


def _missing_meta(root: Path) -> None:
    write_geometry(root, "dk.zone.dk1").with_suffix(".meta.yaml").unlink()


def _nda_claim_in_main(root: Path) -> None:
    edit_yaml(
        root / "claims" / "dk" / "test.yaml",
        lambda d: d.update(document=DOCUMENT, claims=[{**CLAIM, "sensitivity": "nda"}]),
    )


def _claim_unknown_entity(root: Path) -> None:
    edit_yaml(
        root / "claims" / "dk" / "test.yaml",
        lambda d: d.update(document=DOCUMENT, claims=[{**CLAIM, "entity": "dk.site.nowhere"}]),
    )


def _benchmark_unknown_zone(root: Path) -> None:
    edit_yaml(root / "benchmarks" / "dk.yaml", lambda d: d["benchmarks"][0].update(zone="dk.zone.dk9"))


def _bad_yaml(root: Path) -> None:
    (root / "market_rules" / "dk.yaml").write_text("rules: [unclosed", encoding="utf-8")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_dup_entity, "also declared in"),
        # The contract check (geometries) or the loader's own check may report it first; both name the parent.
        (_unknown_parent, "dk.zone.dk9 is not declared"),
        (_undeclared_geometry, "entity_id dk.site.nowhere is not declared"),
        (_missing_meta, "missing sibling loader_test.meta.yaml"),
        (_nda_claim_in_main, "restricted repository"),
        (_claim_unknown_entity, "core.claim.entity_id dk.site.nowhere"),
        (_benchmark_unknown_zone, "zone_id dk.zone.dk9"),
        (_bad_yaml, "market_rules/dk.yaml"),
    ],
)
def test_validation_rejects(tmp_path: Path, mutate: Callable[[Path], None], message: str) -> None:
    root = make_root(tmp_path)
    mutate(root)
    with pytest.raises(CuratedError) as exc:
        read_curated(root)
    assert any(message in p for p in exc.value.problems), exc.value.problems


def test_restricted_root_is_prefixed_and_may_reference_main(tmp_path: Path) -> None:
    restricted = tmp_path / "restricted"
    add_entities(
        restricted,
        {"id": "dk.site.nda_site", "kind": "site", "parent": "dk.zone.dk1", "name": "Site", "sensitivity": "nda"},
    )
    units = read_curated(REPO_ROOT, restricted)
    (site,) = rows(units, "ref.entity", prefix="restricted:")
    assert site["source_path"] == "restricted:registry/entities/dk/entities.yaml"
    assert site["sensitivity"] == "nda"
    with pytest.raises(CuratedError, match="not a directory"):
        read_curated(REPO_ROOT, tmp_path / "mistyped")


# Database


@pytest.fixture()
def conn() -> Iterator[psycopg.Connection[Any]]:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    c = psycopg.connect(url)
    c.execute("delete from ref.curated_file")
    yield c
    c.rollback()
    c.close()


TABLES_TO_SNAPSHOT = [
    "ref.curated_file",
    "ref.country",
    "ref.entity",
    "ref.entity_alias",
    "ref.entity_geometry",
    "ref.source",
    "ref.equivalence_class",
    "ref.codebook_entry",
    "ref.market_rule",
    "ref.assumption_set",
    "ref.benchmark",
    "core.source_document",
    "core.claim",
]


def snapshot(conn: psycopg.Connection[Any]) -> dict[str, str]:
    out = {}
    for table in TABLES_TO_SNAPSHOT:
        row = conn.execute(
            f"select md5(coalesce(string_agg(t::text, '|' order by t::text), '')) from {table} t"
        ).fetchone()
        out[table] = row[0]  # type: ignore[index]
    return out


def count(conn: psycopg.Connection[Any], table: str, prefix: str) -> int:
    row = conn.execute(f"select count(*) from {table} where source_path like %s", (prefix + "%",)).fetchone()
    return int(row[0])  # type: ignore[index]


def load(conn: psycopg.Connection[Any], root: Path, restricted: Path | None = None, dry_run: bool = False) -> Report:
    return load_units(conn, read_curated(root, restricted), restricted=restricted is not None, dry_run=dry_run)


def assumption_set(root: Path, set_id: str) -> Path:
    path = root / "assumptions" / f"{set_id}.yaml"
    data = {
        "id": set_id,
        "kind": "test",
        "status": "proposed",
        "owners": ["misi"],
        "approved_by": None,
        "approved_on": None,
        "sensitivity": "public",
        "origin": "loader test",
        "parameters": {},
    }
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.mark.db
def test_load_twice_second_run_has_no_changes(conn: psycopg.Connection[Any], tmp_path: Path) -> None:
    root = make_root(tmp_path)
    first = load(conn, root)
    assert first.changes == len(read_curated(root))  # ref.curated_file was emptied: every file is new
    expected = yaml_counts(root)
    assert count(conn, "ref.country", "registry/countries/") == expected["ref.country"]
    assert count(conn, "ref.entity", "registry/") == expected["ref.entity"]
    assert count(conn, "ref.codebook_entry", "codebook/") == expected["ref.codebook_entry"]
    assert count(conn, "ref.market_rule", "market_rules/") == expected["ref.market_rule"]
    assert count(conn, "ref.assumption_set", "assumptions/") == expected["ref.assumption_set"]
    assert count(conn, "ref.benchmark", "benchmarks/") == expected["ref.benchmark"]
    classes = list(_yaml(root / "codebook" / "classes.yaml")["classes"])
    row = conn.execute("select count(*) from ref.equivalence_class where class = any(%s)", (classes,)).fetchone()
    assert row is not None and row[0] == expected["ref.equivalence_class"]

    second = load(conn, root)
    assert second.changes == 0, second.text()
    assert all(not f.new and not f.changed and not f.deleted for f in second.folders)
    assert count(conn, "ref.market_rule", "market_rules/") == expected["ref.market_rule"]


@pytest.mark.db
def test_dry_run_writes_nothing(conn: psycopg.Connection[Any], tmp_path: Path) -> None:
    root = make_root(tmp_path)
    add_entities(root, {"id": "dk.site.loader_dry", "kind": "site", "parent": "dk.zone.dk1", "name": "Dry"})
    write_geometry(root, "dk.site.loader_dry")
    before = snapshot(conn)
    report = load(conn, root, dry_run=True)
    assert report.changes > 0
    assert "would upsert" in report.text()
    assert snapshot(conn) == before


@pytest.mark.db
def test_changed_files_update_rows_and_removals_respect_references(
    conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    root = make_root(tmp_path)
    add_entities(
        root,
        {"id": "dk.site.loader_a", "kind": "site", "parent": "dk.zone.dk1", "name": "A"},
        {"id": "dk.site.loader_b", "kind": "site", "parent": "dk.site.loader_a", "name": "B"},
        {"id": "dk.site.loader_c", "kind": "site", "parent": "dk.zone.dk1", "name": "C"},
    )
    referenced, free = assumption_set(root, "loader_referenced"), assumption_set(root, "loader_free")
    load(conn, root)
    # Data now references one assumption set, so it must survive its file's removal.
    run_id = uuid.uuid4()
    conn.execute(
        "insert into ops.compute_run (run_id, kind, name, status, code_version) "
        "values (%s, 'score', 'test', 'ok', 'test')",
        (run_id,),
    )
    conn.execute(
        "insert into feat.score (score_kind, entity_id, period_start, period_end, value, breakdown, "
        "weights_set_id, run_id, code_version) values ('congestion', 'dk.zone.dk1', '2025-01-01', '2025-02-01', "
        "50, '{}', 'loader_referenced', %s, 'test')",
        (run_id,),
    )

    def drop_a_b_rename_c(d: Any) -> None:
        d["entities"] = [e for e in d["entities"] if e["id"] not in ("dk.site.loader_a", "dk.site.loader_b")]
        next(e for e in d["entities"] if e["id"] == "dk.site.loader_c")["name"] = "C renamed"

    edit_yaml(root / "registry" / "entities" / "dk" / "entities.yaml", drop_a_b_rename_c)
    referenced.unlink()
    free.unlink()
    edit_yaml(root / "market_rules" / "dk.yaml", lambda d: d["rules"][0].update(note="changed by test"))

    report = load(conn, root)
    by_folder = {f.folder: f for f in report.folders}
    assert [u.path for u in by_folder["entities"].changed] == ["registry/entities/dk/entities.yaml"]
    # B is A's child: deleting A first fails, so the loader retries after B.
    assert set(by_folder["entities"].deleted) == {"ref.entity dk.site.loader_a", "ref.entity dk.site.loader_b"}
    assert by_folder["assumptions"].removed_files == [
        "assumptions/loader_free.yaml",
        "assumptions/loader_referenced.yaml",
    ]
    assert by_folder["assumptions"].deleted == ["ref.assumption_set loader_free"]
    assert by_folder["assumptions"].kept == ["ref.assumption_set loader_referenced"]
    assert report.changes == 4

    name = conn.execute("select name from ref.entity where entity_id = 'dk.site.loader_c'").fetchone()
    assert name == ("C renamed",)
    gone = conn.execute("select count(*) from ref.entity where entity_id in ('dk.site.loader_a', 'dk.site.loader_b')")
    assert gone.fetchone() == (0,)
    sets = conn.execute("select set_id from ref.assumption_set where set_id like 'loader%'").fetchall()
    assert sets == [("loader_referenced",)]
    notes = conn.execute("select note from ref.market_rule where source_path = 'market_rules/dk.yaml'").fetchall()
    assert ("changed by test",) in notes
    assert len(notes) == len(_yaml(root / "market_rules" / "dk.yaml")["rules"])
    assert load(conn, root).changes == 0


@pytest.mark.db
def test_geometries_and_claims_load(conn: psycopg.Connection[Any], tmp_path: Path) -> None:
    root = make_root(tmp_path)
    add_entities(root, {"id": "dk.site.loader_geo", "kind": "site", "parent": "dk.zone.dk1", "name": "Geo"})
    path = write_geometry(root, "dk.site.loader_geo")
    edit_yaml(root / "claims" / "dk" / "loader_test.yaml", lambda d: d.update(document=DOCUMENT, claims=[CLAIM]))
    load(conn, root)

    geom = conn.execute(
        "select st_geometrytype(geom), st_srid(geom), st_area(geom), source from ref.entity_geometry "
        "where entity_id = 'dk.site.loader_geo'"
    ).fetchone()
    assert geom == ("ST_Polygon", 4326, 1.0, path.relative_to(root).as_posix())
    claim = conn.execute(
        "select c.value_num, c.value_text, c.status::text, c.entity_id, d.source from core.claim c "
        "join core.source_document d using (document_id) where c.claim_id = %s",
        (CLAIM["id"],),
    ).fetchone()
    assert claim == (640.0, None, "pending_review", "dk.zone.dk1", "test_source")

    # Moving the square is a changed file; the geometry row follows.
    moved = {"type": "Point", "coordinates": [10.0, 56.0]}
    write_geometry(root, "dk.site.loader_geo", geometry=moved)
    assert load(conn, root).changes == 1
    point = conn.execute(
        "select st_astext(geom) from ref.entity_geometry where entity_id = 'dk.site.loader_geo'"
    ).fetchone()
    assert point == ("POINT(10 56)",)


@pytest.mark.db
def test_restricted_files_load_prefixed_and_are_left_alone_without_the_path(
    conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    root = make_root(tmp_path)
    restricted = tmp_path / "restricted"
    add_entities(
        restricted,
        {"id": "dk.site.loader_nda", "kind": "site", "parent": "dk.zone.dk1", "name": "NDA", "sensitivity": "nda"},
    )
    load(conn, root, restricted)
    row = conn.execute(
        "select sensitivity::text, source_path from ref.entity where entity_id = 'dk.site.loader_nda'"
    ).fetchone()
    assert row == ("nda", "restricted:registry/entities/dk/entities.yaml")
    assert load(conn, root).changes == 0  # restricted path unset: its files are not treated as removed
    assert load(conn, root, restricted).changes == 0


@pytest.mark.db
def test_scenarios_and_method_cards_load_and_the_official_status_can_move(
    conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """Scenario files and method cards load like other curated files. At most one scenario is official; the
    check waits for the end of the load, so the status can move from one file to another in one load."""
    root = make_root(tmp_path)
    load(conn, root)
    cards = conn.execute("select method_id, version, body from ref.method_card").fetchall()
    assert {"congestion_score", "bess_v0_daily_lp_day_ahead"} <= {m for m, _, _ in cards}
    assert all(v >= 1 and body.strip() for _, v, body in cards)
    scenarios = root / "assumptions" / "scenarios"

    def official() -> list[str]:
        conn.execute("set constraints ref.one_official_scenario immediate")  # run the deferred check now
        conn.execute("set constraints ref.one_official_scenario deferred")  # as at the start of a transaction
        return [s for (s,) in conn.execute("select scenario_id from ref.scenario where status = 'official'")]

    (first,) = official()  # whichever scenario the owners made official
    for now_official, other in (("spec_v0", first), ("demo_default", "spec_v0")):
        edit_yaml(scenarios / f"{other}.yaml", lambda d: d.update(status="exploratory"))
        edit_yaml(scenarios / f"{now_official}.yaml", lambda d: d.update(status="official"))
        load(conn, root)
        assert official() == [now_official]
    sets = conn.execute("select sets from ref.scenario where scenario_id = 'spec_v0'").fetchone()
    assert sets is not None and sets[0]["score_method"] == "score_method_v0"
    with pytest.raises(psycopg.errors.ExclusionViolation):
        conn.execute("update ref.scenario set status = 'official' where scenario_id = 'spec_v0'")
        official()


@pytest.mark.db
def test_migrations_are_applied_once(conn: psycopg.Connection[Any]) -> None:
    from scanner.runtime.migrate import migrate

    assert migrate(os.environ["TEST_DATABASE_URL"], log=lambda _: None) == []
