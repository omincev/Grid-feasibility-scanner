"""Normalisation rules shown on the ingestion assets (scanner.assets.rules) and the dossiers' Decisions."""

from __future__ import annotations

from dagster import AssetKey

from scanner import REPO_ROOT
from scanner.assets.keys import CAPACITY_MAP_KEY, ingest_key, ingested, snapshot_key
from scanner.assets.rules import DECISIONS, dossier_path, ingest_rules, section
from scanner.assets.snapshots import PARSER_DOSSIERS
from scanner.core.registry import load_snapshots
from scanner.runtime.context import RepoState
from scanner.runtime.snapshot_parsers import PARSERS


def test_section_reads_one_level_two_section() -> None:
    text = "# T\n\n## A\n\nfirst\n\n### Sub\n\nstill A\n\n## Decisions\n\n- one\n- two\n\n## B\n\nlater\n"
    assert section(text, "A") == "first\n\n### Sub\n\nstill A"
    assert section(text, DECISIONS) == "- one\n- two"
    assert section(text, "Missing") is None
    assert section("## Decisions\r\n- last\r\n", DECISIONS) == "- last"


def test_every_ingested_source_has_a_dossier_with_decisions(state: RepoState) -> None:
    sources = {m.source for m in state.manifests if ingested(m)}
    assert sources >= {"energidataservice", "entsoe"}
    for source in sources:
        path = dossier_path(REPO_ROOT, source)
        assert path is not None, source
        assert section(path.read_text(encoding="utf-8"), DECISIONS), f"{path} has no Decisions section"
    for parser in PARSERS:
        assert section((REPO_ROOT / PARSER_DOSSIERS[parser]).read_text(encoding="utf-8"), DECISIONS), parser


def test_ingest_assets_show_their_normalisation_rules(state: RepoState) -> None:
    from scanner.ingestion_definitions import defs

    graph = defs.resolve_asset_graph()
    for m in (m for m in state.manifests if ingested(m)):
        node = graph.get(ingest_key(m))
        description, metadata = node.description or "", node.metadata
        assert f"`{m.time_field_utc}` is the interval start in UTC" in description
        assert m.lookback in description and m.lookahead in description
        assert ("per period" in description) == m.per_period_resolution
        for _, e in (state.entries[i] for i in m.entries):
            assert f"| {e.id} | {e.field} |" in description and e.unit in description
        assert metadata["time_field_utc"] == m.time_field_utc and metadata["resolution"] == m.resolution
        assert metadata["fingerprint"].data == m.fingerprint.model_dump()
        assert metadata["dossier"].path == dossier_path(REPO_ROOT, m.source).relative_to(REPO_ROOT).as_posix()  # type: ignore[union-attr]
        assert "decisions" in metadata and "Decisions" in description
    imbalance = next(m for m in state.manifests if m.dataset == "ImbalancePrice")
    description, metadata = ingest_rules(state, imbalance)
    assert "non-positive (downward energy published as zero or negative)" in description
    assert "aFRRDownMW is stored zero or negative" in metadata["decisions"].value.replace("`", "")
    prices = next(m for m in state.manifests if m.dataset == "EnergyPrices")
    description, metadata = ingest_rules(state, prices)
    assert metadata["precedence"].data["day_ahead_price"]["dk"] == ["energidataservice", "entsoe"]
    assert "dk: energidataservice > **entsoe**" in description
    assert "A03 expansion" in metadata["decisions"].value and "SDAC sequence" in metadata["decisions"].value


def test_capacity_map_and_parsed_snapshots_show_their_dossiers() -> None:
    from scanner.ingestion_definitions import defs

    graph = defs.resolve_asset_graph()
    capacity = graph.get(CAPACITY_MAP_KEY)
    assert "Null is not zero" in capacity.metadata["decisions"].value
    assert "percent fields are not ingested" in capacity.metadata["decisions_2"].value.lower()
    queue = next(s for s in load_snapshots(REPO_ROOT) if s.on_change == "parse:connection_queue")
    node = graph.get(AssetKey(snapshot_key(queue).path))
    assert node.metadata["dossier"].path == PARSER_DOSSIERS["connection_queue"]
