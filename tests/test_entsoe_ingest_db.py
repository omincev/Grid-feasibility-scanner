"""ENTSO-E through run_ingest and load_input on a real database, offline. Run with TEST_DATABASE_URL set.

Recorded fixtures are served through httpx.MockTransport (zones without a fixture get the recorded
"No matching data found" acknowledgement). FAKE_TOKEN is SYNTHETIC; the tests check it never reaches
raw sidecars, ops.ingest_run.error, the bead outbox or log records. Each test uses its own `source`.
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from conftest import FIXTURES
from pydantic import SecretStr

import scanner.runtime.ingest as ingest
from scanner.adapters.entsoe import EntsoeAdapter, EntsoeHttpError
from scanner.core.manifest import Manifest, load_manifests
from scanner.core.raw_store import RawStore
from scanner.core.registry import load_countries, load_market_rules
from scanner.features.inputs import load_input
from scanner.features.registry import FEATURE_INPUTS
from scanner.runtime.context import RepoState
from scanner.settings import Settings

pytestmark = pytest.mark.db

FAKE_TOKEN = "33333333-synthetic-ingest-token-3333"
DK1, NL = "10YDK-1--------W", "10YNL----------L"
Handler = Callable[[httpx.Request], httpx.Response]


def _xml(name: str) -> bytes:
    return (FIXTURES / "entsoe" / f"{name}.xml").read_bytes()


@pytest.fixture()
def db_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    return url


@pytest.fixture()
def conn(db_url: str) -> Iterator[psycopg.Connection[Any]]:
    c = psycopg.connect(db_url)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _cleanup(db_url: str) -> Iterator[None]:
    yield
    series = "select series_id from core.series where split_part(series_key, '.', 2) ~ '^t[0-9a-f]{10}$'"
    runs = "select run_id from ops.ingest_run where source like 'test\\_%'"
    with psycopg.connect(db_url) as c:
        c.execute(f"delete from core.observation where series_id in ({series})")
        c.execute(f"delete from core.series where series_id in ({series})")
        c.execute(f"delete from ops.fingerprint where run_id in ({runs})")
        c.execute("delete from ops.bead_outbox where source like 'test\\_%'")
        c.execute(f"delete from ops.ingest_run where run_id in ({runs})")
        c.execute("delete from ref.entity where entity_id ~ %s", (TEST_ZONE,))


# Test-only zones (fresh_zone): series of these zones never meet the data a populated database already holds
# for the real zones, which load_input would otherwise read as well.
TEST_ZONE = r"^[a-z]{2}\.zone\.t[0-9a-f]{10}$"


def fresh_zone(cc: str) -> str:
    return f"{cc}.zone.t{uuid.uuid4().hex[:10]}"


def with_nl_zone(state: RepoState, base: Path, zone_id: str) -> RepoState:
    """The repository's country files and ENTSO-E manifests with nl.yaml's zone ID replaced by `zone_id`, so
    NL still enters the manifests through its country file alone."""
    countries, manifests = Path("registry", "countries"), Path("src", "scanner", "adapters", "entsoe", "manifests")
    shutil.copytree(state.root / countries, base / countries)
    shutil.copytree(state.root / manifests, base / manifests)
    nl = base / countries / "nl.yaml"
    text = nl.read_text(encoding="utf-8")
    assert "id: nl.zone.nl," in text
    nl.write_text(text.replace("id: nl.zone.nl,", f"id: {zone_id},"), encoding="utf-8", newline="\n")
    return RepoState(base, load_countries(base), load_manifests(base), state.entries)


@pytest.fixture()
def raw_dir(tmp_path: Path) -> Path:
    return tmp_path / "raw"


@pytest.fixture()
def serve(monkeypatch: pytest.MonkeyPatch, db_url: str, raw_dir: Path) -> Callable[[Handler], None]:
    settings = Settings(database_url=db_url, raw_store_url=f"file://{raw_dir.as_posix()}")
    monkeypatch.setattr(ingest, "get_settings", lambda: settings)

    def install(handler: Handler) -> None:
        client = httpx.Client(transport=httpx.MockTransport(handler))
        token = SecretStr(FAKE_TOKEN)
        monkeypatch.setattr(ingest, "load_adapter", lambda path: EntsoeAdapter(client, token, sleep=lambda s: None))

    return install


def by_zone(fixtures: dict[str, str]) -> Handler:
    """Answer each request with the fixture recorded for its zone, else with "No matching data found"."""

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        eic = params.get("in_Domain") or params.get("outBiddingZone_Domain")
        name = fixtures.get(eic or "", "no_data__day_ahead_prices__dk1__2027-01-15")
        return httpx.Response(200, content=_xml(name), headers={"content-type": "text/xml"})

    return handler


def isolated(state: RepoState, dataset: str, **changes: Any) -> Manifest:
    m = next(m for m in state.manifests if m.source == "entsoe" and m.dataset == dataset)
    tag = uuid.uuid4().hex[:10]
    return m.model_copy(update={"source": f"test_{tag}", "source_short": f"t{tag}", **changes})


def ensure_refs(conn: psycopg.Connection[Any], state: RepoState, m: Manifest) -> None:
    for cc in {"eu", *(z.split(".")[0] for z in m.zones.values())}:
        conn.execute(
            "insert into ref.country (code, name, status, timezone, currency, config, source_path, content_hash) "
            "values (%s, %s, 'reference', 'Europe/Brussels', 'EUR', '{}', 't', 't') on conflict do nothing",
            (cc, cc),
        )
    for zone_id in m.zones.values():
        conn.execute(
            "insert into ref.entity (entity_id, kind, country_code, name) values (%s, 'zone', %s, %s) "
            "on conflict do nothing",
            (zone_id, zone_id.split(".")[0], zone_id),
        )
    for e in state.entries_for(m):
        conn.execute(
            "insert into ref.equivalence_class values (%s, 'x', 'x') on conflict do nothing", (e.equivalence_class,)
        )
        conn.execute(
            "insert into ref.codebook_entry (entry_id, source, dataset, country_code, field, equivalence_class, kind, "
            "unit, resample, definition, status, source_path, content_hash) "
            "values (%s, 'entsoe', %s, 'eu', %s, %s, %s, %s, %s, '{}', 'draft', 't', 't') on conflict do nothing",
            (e.id, m.dataset, e.field, e.equivalence_class, e.kind, e.unit, e.resample),
        )
    conn.commit()


def stored_text(raw_dir: Path) -> str:
    return "".join(p.read_bytes().decode("utf-8", "replace") for p in raw_dir.rglob("*") if p.is_file())


def precedence(state: RepoState) -> dict[str, dict[str, list[str]]]:
    return {cc: c.precedence for cc, c in state.countries.items()}


def test_nl_prices_ingest_and_load_from_the_country_file_alone(
    state: RepoState, conn: psycopg.Connection[Any], serve: Callable[[Handler], None], raw_dir: Path, tmp_path: Path
) -> None:
    """ADR 0011 / WP3.2: NL is in the manifest only through registry/countries/nl.yaml, and its prices
    come back out of load_input through nl.yaml's precedence, with no NL-specific code. nl.yaml's zone is a
    test zone here (tests/test_agnostic.py checks the real file), so real nl.zone.nl data is never read."""
    zone = fresh_zone("nl")
    nl_state = with_nl_zone(state, tmp_path / "repo", zone)
    m = isolated(nl_state, "EnergyPrices")
    assert m.zones[NL] == zone
    ensure_refs(conn, nl_state, m)
    serve(by_zone({NL: "day_ahead_prices__nl__2026-10-14"}))
    out = ingest.run_ingest(nl_state, m, date(2026, 10, 14))
    assert out.status == "ok" and out.severity == "none", out.deviations
    assert out.rows_written == out.rows_received == out.rows_expected == 96
    assert len(out.raw_uris) == len(m.zones)  # one response per zone, all stored
    text = stored_text(raw_dir)
    assert FAKE_TOKEN not in text and '"in_Domain": "10YNL----------L"' in text

    (resolution,) = conn.execute(
        "select resolution from core.series where series_key = %s",
        (f"eu.{m.source_short}.EnergyPrices.{zone}.price_amount.pt15m",),
    ).fetchone() or (None,)
    assert resolution == timedelta(minutes=15)
    start, end = datetime(2026, 10, 13, 22, tzinfo=UTC), datetime(2026, 10, 14, 22, tzinfo=UTC)
    df = load_input(
        conn,
        ("day_ahead_price",),
        FEATURE_INPUTS["f1_price_spread"]["price"],
        [zone],
        start,
        end,
        precedence=precedence(nl_state),
    )
    assert df.height == 96 and df["unit"].unique().to_list() == ["EUR/MWh"]


def test_resolution_switch_is_two_series_that_load_as_one(
    state: RepoState, conn: psycopg.Connection[Any], serve: Callable[[Handler], None]
) -> None:
    zone = fresh_zone("dk")  # stands in for DK1; real dk.zone.dk1 prices would be loaded as well
    m = isolated(state, "EnergyPrices", zones={DK1: zone})
    ensure_refs(conn, state, m)
    serve(by_zone({DK1: "day_ahead_prices__dk1__2025-09-30_2025-10-01_resolution_switch"}))
    out = ingest.run_ingest(state, m, date(2025, 10, 1))
    assert out.status == "ok", out.deviations
    assert out.rows_written == 24 + 96 and out.rows_received == out.rows_expected == 96
    stored = dict(
        conn.execute(
            "select split_part(series_key, '.', -1), resolution from core.series where series_key like %s",
            (f"eu.{m.source_short}.%",),
        ).fetchall()
    )
    assert stored == {"pt1h": timedelta(hours=1), "pt15m": timedelta(minutes=15)}

    start, end = datetime(2025, 9, 29, 22, tzinfo=UTC), datetime(2025, 10, 1, 22, tzinfo=UTC)
    spec = FEATURE_INPUTS["f1_price_spread"]["price"]
    for rules in (None, load_market_rules(state.root)["dk"]):  # with and without the DK market rule
        df = load_input(
            conn,
            ("day_ahead_price",),
            spec,
            [zone],
            start,
            end,
            rules=rules,
            tz="Europe/Copenhagen",
            precedence=precedence(state),
        )
        assert df.height == 120 and df["entity_id"].unique().to_list() == [zone]
        lengths = (df["interval_end"] - df["interval_start"]).unique().sort().to_list()
        assert lengths == [timedelta(minutes=15), timedelta(hours=1)]


def test_load_and_generation_for_an_active_zone(
    state: RepoState, conn: psycopg.Connection[Any], serve: Callable[[Handler], None]
) -> None:
    for dataset, case, written in (
        ("ActualTotalLoad", "actual_total_load__dk1__2026-10-14", 15),
        ("ActualGenerationPerType", "actual_generation_per_type__dk1__2026-10-14", 3 * 96),
    ):
        m = isolated(state, dataset)
        ensure_refs(conn, state, m)
        serve(by_zone({DK1: case}))
        out = ingest.run_ingest(state, m, date(2026, 10, 14))
        assert out.status == "ok", out.deviations
        assert out.rows_written == out.rows_received == written
        if dataset == "ActualTotalLoad":
            assert out.rows_expected == 24  # 9 hours the source did not publish stay missing


def test_past_day_without_any_data_blocks(
    state: RepoState, conn: psycopg.Connection[Any], serve: Callable[[Handler], None]
) -> None:
    m = isolated(state, "ActualTotalLoad")
    ensure_refs(conn, state, m)
    serve(by_zone({}))
    out = ingest.run_ingest(state, m, date(2026, 10, 14))
    assert out.status == "blocked" and "hard:content:zero rows for a past day" in out.deviations
    assert conn.execute("select count(*) from ops.bead_outbox where source = %s", (m.source,)).fetchone() == (1,)


def test_token_never_reaches_error_bead_or_logs(
    state: RepoState, conn: psycopg.Connection[Any], serve: Callable[[Handler], None], caplog: pytest.LogCaptureFixture
) -> None:
    m = isolated(state, "ActualTotalLoad")
    ensure_refs(conn, state, m)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused {request.url}&securityToken={FAKE_TOKEN}", request=request)

    serve(refuse)
    with caplog.at_level(logging.DEBUG), pytest.raises(EntsoeHttpError):
        ingest.run_ingest(state, m, date(2026, 10, 14))
    ((error,),) = conn.execute("select error from ops.ingest_run where source = %s", (m.source,)).fetchall()
    ((title, body),) = conn.execute("select title, body from ops.bead_outbox where source = %s", (m.source,)).fetchall()
    assert "hard:transport:EntsoeHttpError" in error and "securityToken=<redacted>" in error
    for text in (error, title, body, *(r.getMessage() for r in caplog.records)):
        assert FAKE_TOKEN not in text


def test_backfill_run_is_served_from_a_reused_wide_response(
    state: RepoState,
    conn: psycopg.Connection[Any],
    serve: Callable[[Handler], None],
    raw_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A past window is fetched wide once per zone; a re-run is served from it, each run stores its raw
    capture, and the fingerprinted partition day and the rows written are those of the narrow fetch."""
    import json

    from scanner.adapters.entsoe import adapter as adapter_module

    monkeypatch.setattr(adapter_module, "REUSE_MIN_AGE", timedelta(0))  # the fixture day counts as past
    m = isolated(state, "ActualTotalLoad")
    ensure_refs(conn, state, m)
    seen: list[httpx.Request] = []
    inner = by_zone({DK1: "actual_total_load__dk1__2026-10-14"})

    def counting(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return inner(request)

    serve(counting)
    first = ingest.run_ingest(state, m, date(2026, 10, 14))
    assert first.status == "ok", first.deviations
    assert first.rows_written == first.rows_received == 15 and first.rows_expected == 24
    assert len(seen) == len(m.zones)
    second = ingest.run_ingest(state, m, date(2026, 10, 14))
    assert second.status == "ok" and len(seen) == len(m.zones)  # no new request
    metas = sorted(raw_dir.rglob("*.meta.json"))
    assert len(metas) == 2 * len(m.zones)  # each run keeps a reference and a sidecar per zone
    sidecars = [json.loads(f.read_text(encoding="utf-8")) for f in metas]
    # The reused responses are stored once: DK1's fixture and the acknowledgement the other zones share.
    captures = [p for p in (raw_dir / "raw").rglob("*") if p.is_file() and not p.name.endswith(".meta.json")]
    assert len(captures) == len({s["sha256"] for s in sidecars}) == 2
    store = RawStore(f"file://{raw_dir.as_posix()}")
    assert [store.get(u) for u in second.raw_uris] == [store.get(u) for u in first.raw_uris]
    spans = {(p["periodStart"], p["periodEnd"]) for p in (s["params"] for s in sidecars)}
    # One wide window, from the run's fetch start (day - 2 days) to the cutoff (here: now).
    assert len(spans) == 1 and next(iter(spans))[0] == "202610112200"
    assert next(iter(spans))[1] > "202610142200"
    assert FAKE_TOKEN not in stored_text(raw_dir)
