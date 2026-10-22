"""run_ingest end to end on a real database, offline (recorded fixtures through httpx.MockTransport).

WP1.6: hard deviations block the write and draft exactly one bead for every ingest asset; plus the
lookahead window, comparable row counts, transport failures and which rows a soft deviation quarantines
(docs/quality.md layer 2). Run with TEST_DATABASE_URL set and
migrations applied. Each test uses its own `source` so fingerprint baselines and series never mix.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from conftest import FIXTURES

import scanner.runtime.ingest as ingest
from scanner.adapters.base import ParseError
from scanner.adapters.energidataservice import EnergiDataServiceAdapter
from scanner.adapters.energidataservice import adapter as adapter_module
from scanner.core.manifest import FingerprintConfig, Manifest
from scanner.core.raw_store import RawStore
from scanner.core.time import local_day, local_day_bounds
from scanner.runtime import quarantine
from scanner.runtime.context import RepoState
from scanner.settings import Settings

pytestmark = pytest.mark.db

EDS = FIXTURES / "energidataservice"
Handler = Callable[[httpx.Request], httpx.Response]


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
    """Delete what each test wrote. run_ingest commits on its own connection, so no rollback can undo it,
    and leftover series of real codebook entries would leak into other DB tests (e.g. feature inputs)."""
    yield
    series = "select series_id from core.series where split_part(series_key, '.', 2) ~ '^t[0-9a-f]{10}$'"
    runs = "select run_id from ops.ingest_run where source like 'test\\_%'"
    with psycopg.connect(db_url) as c:
        c.execute(f"delete from core.observation where series_id in ({series})")
        c.execute(f"delete from core.series where series_id in ({series})")
        c.execute(f"delete from ops.fingerprint where run_id in ({runs})")
        c.execute("delete from ops.bead_outbox where source like 'test\\_%'")
        c.execute(f"delete from ops.ingest_run where run_id in ({runs})")


@pytest.fixture()
def serve(monkeypatch: pytest.MonkeyPatch, db_url: str, tmp_path: Path) -> Callable[[Handler], None]:
    """Point run_ingest at the test database, a temporary raw store and a mocked EDS API."""
    settings = Settings(database_url=db_url, raw_store_url=f"file://{tmp_path.as_posix()}/raw")
    monkeypatch.setattr(ingest, "get_settings", lambda: settings)
    monkeypatch.setattr(adapter_module, "_retired_responses", {})

    def install(handler: Handler) -> None:
        client = httpx.Client(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(ingest, "load_adapter", lambda path: EnergiDataServiceAdapter(client, sleep=lambda s: None))

    return install


def fixture_response(name: str, mutate: Callable[[dict[str, Any]], None] | None = None) -> Handler:
    body = json.loads((EDS / f"{name}.json").read_bytes())
    if mutate:
        for rec in body["records"]:
            mutate(rec)
    content = json.dumps(body).encode()
    return lambda request: httpx.Response(200, content=content, headers={"content-type": "application/json"})


def isolated(state: RepoState, dataset: str, **changes: Any) -> Manifest:
    """The manifest under a unique source, with refs inserted so its series can be created."""
    m = next(m for m in state.manifests if m.dataset == dataset)
    tag = uuid.uuid4().hex[:10]
    return m.model_copy(update={"source": f"test_{tag}", "source_short": f"t{tag}", **changes})


def ensure_refs(conn: psycopg.Connection[Any], state: RepoState, m: Manifest) -> None:
    for zone_id in m.zones.values():
        cc = zone_id.split(".")[0]
        conn.execute(
            "insert into ref.country (code, name, status, timezone, currency, config, source_path, content_hash) "
            "values (%s, %s, 'active', 'Europe/Copenhagen', 'EUR', '{}', 't', 't') on conflict do nothing",
            (cc, cc),
        )
        conn.execute(
            "insert into ref.entity (entity_id, kind, country_code, name) values (%s, 'zone', %s, %s) "
            "on conflict do nothing",
            (zone_id, cc, zone_id),
        )
    for e in state.entries_for(m):
        conn.execute(
            "insert into ref.equivalence_class values (%s, 'x', 'x') on conflict do nothing", (e.equivalence_class,)
        )
        conn.execute(
            "insert into ref.codebook_entry (entry_id, source, dataset, country_code, field, equivalence_class, kind, "
            "unit, resample, definition, status, source_path, content_hash) "
            "values (%s, 'energidataservice', %s, 'dk', %s, %s, %s, %s, %s, '{}', 'draft', 't', 't') "
            "on conflict do nothing",
            (e.id, m.dataset, e.field, e.equivalence_class, e.kind, e.unit, e.resample),
        )
    conn.commit()


def run_row(conn: psycopg.Connection[Any], run_id: str) -> dict[str, Any]:
    cur = conn.execute("select * from ops.ingest_run where run_id = %s", (run_id,))
    names = [d.name for d in cur.description or []]
    row = cur.fetchone()
    assert row is not None
    return dict(zip(names, row, strict=True))


def beads(conn: psycopg.Connection[Any], m: Manifest) -> list[tuple[str, str, str | None]]:
    return conn.execute(
        "select title, body, raw_uri from ops.bead_outbox where source = %s order by outbox_id", (m.source,)
    ).fetchall()


def observations(conn: psycopg.Connection[Any], run_id: str) -> int:
    row = conn.execute("select count(*) from core.observation where run_id = %s", (run_id,)).fetchone()
    assert row is not None
    return int(row[0])


def test_lookahead_writes_tomorrow_and_counts_compare(
    state: RepoState, conn: psycopg.Connection[Any], serve: Callable[[Handler], None]
) -> None:
    """Bug b and c: the run for D writes D+1 prices published on D; received and expected count D only."""
    m = isolated(state, "DayAheadPrices", zones={"DK1": "dk.zone.dk1", "DK2": "dk.zone.dk2"})
    ensure_refs(conn, state, m)
    serve(fixture_response("DayAheadPrices__lookahead"))  # 2026-10-17 and 2026-10-18
    day = date(2026, 10, 17)
    out = ingest.run_ingest(state, m, day)
    assert out.status == "ok", out.deviations
    assert out.rows_written == 2 * 96 * 2  # D and D+1, two zones
    assert out.rows_received == out.rows_expected == 96 * 2  # the partition day only
    run = run_row(conn, out.run_id)
    w = ingest.ingest_windows(m, "Europe/Copenhagen", day)
    assert run["window_end"] == w.fetch_end and run["window_start"] == w.fetch_start
    fp = conn.execute("select payload from ops.fingerprint where run_id = %s", (out.run_id,)).fetchone()
    assert fp is not None and fp[0]["rows"] == 96 * 2

    # With the partition one day later and a lookback over the first day, the counts still compare.
    m2 = m.model_copy(update={"lookback": "P1D", "lookahead": "P0D"})
    out2 = ingest.run_ingest(state, m2, day + timedelta(days=1))
    assert out2.status == "ok" and out2.rows_received == 96 * 2 and out2.rows_written == 0  # unchanged values


def test_retired_backfill_stores_the_reused_response_once(
    state: RepoState, conn: psycopg.Connection[Any], serve: Callable[[Handler], None], tmp_path: Path
) -> None:
    """A retired dataset's 31-day response serves the next daily runs of the process: its bytes are stored
    once, and each run keeps its own reference and sidecar, which read them back."""
    m = isolated(state, "Elspotprices", zones={"DK1": "dk.zone.dk1", "DK2": "dk.zone.dk2"})
    ensure_refs(conn, state, m)
    requests: list[httpx.Request] = []
    respond = fixture_response("Elspotprices__normal_day")

    def counting(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return respond(request)

    serve(counting)
    first = ingest.run_ingest(state, m, date(2025, 6, 10))
    second = ingest.run_ingest(state, m, date(2025, 6, 11))
    assert len(requests) == 1
    (a,), (b,) = first.raw_uris, second.raw_uris
    assert a.startswith("raw://") and a != b and run_row(conn, second.run_id)["raw_uris"] == [b]
    store = RawStore(f"file://{tmp_path.as_posix()}/raw")
    assert store.get(b) == store.get(a) and store.sidecar(b)["content_uri"] == a
    names = [p.name for p in (tmp_path / "raw" / "raw").rglob("*") if p.is_file()]
    assert len(names) == 3 and sum(n.endswith(".meta.json") for n in names) == 2  # one response, two sidecars


SPIKE_DAY = date(2026, 2, 17)  # DayAheadPrices__spike: 2026-02-16 to 2026-02-18, the 911.14 EUR/MWh spike in DK2


def spike_manifest(state: RepoState, conn: psycopg.Connection[Any]) -> Manifest:
    """DayAheadPrices as configured (lookback P3D, lookahead P1D) for DK1 and DK2, with soft checks from
    the second run on, so the spike day compared with 2026-02-16 is a soft deviation."""
    zones = {"DK1": "dk.zone.dk1", "DK2": "dk.zone.dk2"}
    m = isolated(state, "DayAheadPrices", zones=zones, fingerprint=FingerprintConfig(warmup_runs=1))
    ensure_refs(conn, state, m)
    return m


def latest_by_day(conn: psycopg.Connection[Any], m: Manifest) -> dict[date, set[tuple[str, str, str]]]:
    """(review_status, quality, run_id) of the latest versions of the manifest's rows, per local day."""
    rows = conn.execute(
        "select e.interval_start, e.review_status, e.quality::text, e.run_id::text from core.observation_effective e "
        "join core.series s on s.series_id = e.series_id where s.series_key like %s",
        (f"%.{m.source_short}.%",),
    ).fetchall()
    out: dict[date, set[tuple[str, str, str]]] = {}
    for start, status, quality, run_id in rows:
        out.setdefault(local_day(start, "Europe/Copenhagen"), set()).add((status, quality, run_id))
    return out


def run_spike_days(state: RepoState, m: Manifest) -> tuple[ingest.IngestOutcome, ingest.IngestOutcome]:
    """Run D-1 (clean) writes D through its lookahead as ok; run D fingerprints the spike day (soft)."""
    before = ingest.run_ingest(state, m, SPIKE_DAY - timedelta(days=1))
    assert (before.status, before.severity) == ("ok", "none"), before.deviations
    assert before.rows_written == 2 * 96 * 2  # D-1 and D (lookahead); the fixture has no earlier lookback
    spike = ingest.run_ingest(state, m, SPIKE_DAY)
    assert (spike.status, spike.severity) == ("ok", "soft")
    assert any(d.startswith("soft:content:DayAheadPriceEUR range [79.0, 911.140015]") for d in spike.deviations)
    assert spike.rows_received == spike.rows_expected == 96 * 2
    return before, spike


def test_soft_deviation_labels_the_fingerprinted_day(
    state: RepoState, conn: psycopg.Connection[Any], serve: Callable[[Handler], None]
) -> None:
    """The flagged day gets a quarantined version of its unchanged values; the day after stays ok."""
    m = spike_manifest(state, conn)
    serve(fixture_response("DayAheadPrices__spike"))
    before, spike = run_spike_days(state, m)
    assert spike.rows_written == 96 * 2 + 96 * 2  # D relabelled, D+1 new; D-1 (lookback) unchanged
    d = SPIKE_DAY
    assert latest_by_day(conn, m) == {
        d - timedelta(days=1): {("ok", "ok", before.run_id)},
        d: {("unverified", "quarantined", spike.run_id)},
        d + timedelta(days=1): {("ok", "ok", spike.run_id)},
    }
    # Append-only: run D-1's ok versions of day D remain, with the same values as the quarantined ones.
    versions = conn.execute(
        "select o.quality::text, count(*), sum(o.value) from core.observation o "
        "join core.series s on s.series_id = o.series_id "
        "where s.series_key like %s and o.interval_start >= %s and o.interval_start < %s group by 1 order by 1",
        (f"%.{m.source_short}.%", *local_day_bounds(d, "Europe/Copenhagen")),
    ).fetchall()
    assert [(q, n) for q, n, _ in versions] == [("ok", 192), ("quarantined", 192)]
    assert versions[0][2] == pytest.approx(versions[1][2])
    # Review targets the run that fingerprinted the day, over exactly that day.
    (listed,) = [r for r in quarantine.quarantined_runs(conn, m.dataset) if r.source == m.source]
    assert (listed.run_id, listed.rows, listed.superseded) == (spike.run_id, 192, 0)
    assert listed.span == local_day_bounds(d, "Europe/Copenhagen")


def test_clean_run_through_lookback_keeps_the_label(
    state: RepoState, conn: psycopg.Connection[Any], serve: Callable[[Handler], None]
) -> None:
    """Run D+1 re-fetches D through its lookback with a clean fingerprint: unchanged values keep their
    label (only a decision clears it); a value the source revised is written ok."""
    m = spike_manifest(state, conn)
    serve(fixture_response("DayAheadPrices__spike"))
    _, spike = run_spike_days(state, m)
    after = ingest.run_ingest(state, m, SPIKE_DAY + timedelta(days=1))
    assert (after.status, after.severity, after.rows_written) == ("ok", "none", 0), after.deviations
    assert latest_by_day(conn, m)[SPIKE_DAY] == {("unverified", "quarantined", spike.run_id)}

    revised = ("2026-02-17T12:30:00", "DK1")  # 79.0 at the source

    def revise(rec: dict[str, Any]) -> None:
        if (rec["TimeUTC"], rec["PriceArea"]) == revised:
            rec["DayAheadPriceEUR"] = 80.0

    serve(fixture_response("DayAheadPrices__spike", revise))
    again = ingest.run_ingest(state, m, SPIKE_DAY + timedelta(days=1))
    assert (again.status, again.severity, again.rows_written) == ("ok", "none", 1), again.deviations
    assert latest_by_day(conn, m)[SPIKE_DAY] == {
        ("unverified", "quarantined", spike.run_id),
        ("ok", "ok", again.run_id),
    }


def test_overlapping_runs_of_one_dataset_keep_the_later_fetch(
    state: RepoState, conn: psycopg.Connection[Any], serve: Callable[[Handler], None]
) -> None:
    """Two runs of one dataset at the same time (a UI backfill next to a schedule tick: Dagster's per-source
    limit applies only to runs tagged by the schedule's job). Run A fetches, run B fetches a revised value
    after it; B's revision must stay the latest version, not A's older fetch written last."""
    m = spike_manifest(state, conn)
    revised = ("2026-02-17T12:30:00", "DK1")  # 79.0 at the source, then revised to 80.0

    def revise(rec: dict[str, Any]) -> None:
        if (rec["TimeUTC"], rec["PriceArea"]) == revised:
            rec["DayAheadPriceEUR"] = 80.0

    stale, fresh = fixture_response("DayAheadPrices__spike"), fixture_response("DayAheadPrices__spike", revise)
    a_fetching, b_done = threading.Event(), threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        if threading.current_thread().name == "run-a":
            a_fetching.set()
            b_done.wait(timeout=3)  # B overtakes A here unless runs of one dataset wait for each other
            return stale(request)
        return fresh(request)

    serve(handler)
    outcomes: dict[str, Any] = {}

    def run(name: str) -> None:
        try:
            outcomes[name] = ingest.run_ingest(state, m, SPIKE_DAY)
        except Exception as exc:  # reported by the assertion below
            outcomes[name] = exc
        finally:
            if name == "run-b":
                b_done.set()

    a = threading.Thread(target=run, args=("run-a",), name="run-a")
    a.start()
    assert a_fetching.wait(timeout=10)
    b = threading.Thread(target=run, args=("run-b",), name="run-b")
    b.start()
    a.join(timeout=30)
    b.join(timeout=30)
    assert [getattr(outcomes.get(n), "status", outcomes.get(n)) for n in ("run-a", "run-b")] == ["ok", "ok"]
    latest = conn.execute(
        "select o.value from core.observation_latest o join core.series s on s.series_id = o.series_id "
        "where s.series_key like %s and o.interval_start = %s",
        (f"%.{m.source_short}.%.dk1.%", datetime(2026, 2, 17, 12, 30, tzinfo=UTC)),
    ).fetchall()
    assert latest == [(80.0,)]


def test_revised_value_takes_the_quality_of_its_interval(
    state: RepoState, conn: psycopg.Connection[Any], serve: Callable[[Handler], None]
) -> None:
    """In a soft run, a revision inside the fingerprinted day is quarantined, one in the lookback is ok."""
    m = spike_manifest(state, conn)
    serve(fixture_response("DayAheadPrices__spike"))
    before = ingest.run_ingest(state, m, SPIKE_DAY - timedelta(days=1))
    assert before.status == "ok" and before.severity == "none", before.deviations

    revisions = {("2026-02-16T04:00:00", "DK1"): 61.0, ("2026-02-17T12:30:00", "DK1"): 80.0}  # 60.099998, 79.0

    def revise(rec: dict[str, Any]) -> None:
        rec["DayAheadPriceEUR"] = revisions.get((rec["TimeUTC"], rec["PriceArea"]), rec["DayAheadPriceEUR"])

    serve(fixture_response("DayAheadPrices__spike", revise))
    spike = ingest.run_ingest(state, m, SPIKE_DAY)
    assert spike.severity == "soft"
    assert spike.rows_written == 1 + 96 * 2 + 96 * 2  # the lookback revision, D (one revised), D+1
    latest = {
        start: rest
        for start, *rest in conn.execute(
            "select o.interval_start, o.value, o.quality::text, o.run_id::text from core.observation_latest o "
            "join core.series s on s.series_id = o.series_id where s.series_key like %s",
            (f"%.{m.source_short}.%.dk1.%",),
        )
    }
    assert latest[datetime(2026, 2, 16, 4, tzinfo=UTC)] == [61.0, "ok", spike.run_id]
    assert latest[datetime(2026, 2, 17, 12, 30, tzinfo=UTC)] == [80.0, "quarantined", spike.run_id]


def test_explicit_nulls_are_stored_as_null(
    state: RepoState, conn: psycopg.Connection[Any], serve: Callable[[Handler], None]
) -> None:
    m = isolated(state, "ImbalancePrice")
    ensure_refs(conn, state, m)
    serve(fixture_response("ImbalancePrice__explicit_nulls"))
    out = ingest.run_ingest(state, m, date(2025, 3, 14))
    assert out.status == "ok", out.deviations
    assert out.rows_received == out.rows_expected == 96 * 2 * len(m.entries)
    nulls = conn.execute(
        "select count(*) from core.observation where run_id = %s and value is null", (out.run_id,)
    ).fetchone()
    assert nulls == (20,)


@pytest.mark.parametrize(
    "dataset", ["DayAheadPrices", "Elspotprices", "ImbalancePrice", "Forecasts_Hour", "GenerationProdTypeExchange"]
)
def test_final_error_status_blocks_write_and_drafts_one_bead(
    state: RepoState, conn: psycopg.Connection[Any], serve: Callable[[Handler], None], dataset: str
) -> None:
    """WP1.6 for every ingest asset: a 5xx that outlives the retries is a hard deviation."""
    m = isolated(state, dataset)
    ensure_refs(conn, state, m)
    serve(lambda request: httpx.Response(503, content=b'{"statusCode": 503, "message": "unavailable"}'))
    out = ingest.run_ingest(state, m, m.history_start_local + timedelta(days=10))
    assert out.status == "blocked" and out.severity == "hard"
    assert any("HTTP status 503" in d for d in out.deviations)
    assert observations(conn, out.run_id) == 0
    run = run_row(conn, out.run_id)
    assert run["status"] == "blocked" and len(run["raw_uris"]) == 1  # the error body is kept
    (bead,) = beads(conn, m)
    assert bead[2] == run["raw_uris"][0] and "hard deviation" in bead[0]


def test_schema_change_blocks_write_and_drafts_one_bead(
    state: RepoState, conn: psycopg.Connection[Any], serve: Callable[[Handler], None]
) -> None:
    m = isolated(state, "GenerationProdTypeExchange")
    ensure_refs(conn, state, m)
    serve(fixture_response("GenerationProdTypeExchange__normal_day"))
    first = ingest.run_ingest(state, m, date(2026, 10, 14))
    assert first.status == "ok" and first.rows_written == 24 * 2 * 4

    serve(fixture_response("GenerationProdTypeExchange__normal_day", lambda r: r.pop("CO2PerkWh")))
    second = ingest.run_ingest(state, m, date(2026, 10, 14))
    assert second.status == "blocked"
    assert "hard:structure:field removed: CO2PerkWh" in second.deviations
    assert observations(conn, second.run_id) == 0
    assert len(beads(conn, m)) == 1


def test_today_without_records_yet_is_not_a_schema_change(
    state: RepoState, conn: psycopg.Connection[Any], serve: Callable[[Handler], None]
) -> None:
    """Just after local midnight today's partition has no ImbalancePrice records yet; that must not
    read as 'every field removed' against the baseline."""
    m = isolated(state, "ImbalancePrice")
    ensure_refs(conn, state, m)
    serve(fixture_response("ImbalancePrice__normal_day"))
    assert ingest.run_ingest(state, m, date(2026, 10, 14)).status == "ok"
    serve(fixture_response("ImbalancePrice__empty"))
    out = ingest.run_ingest(state, m, local_day(datetime.now(UTC), "Europe/Copenhagen"))
    assert out.status == "ok" and out.severity == "none", out.deviations
    assert beads(conn, m) == []
    # The empty run does not become the schema baseline: a later schema change is still caught.
    serve(fixture_response("ImbalancePrice__normal_day", lambda r: r.pop("SpotPriceEUR")))
    assert ingest.run_ingest(state, m, date(2026, 10, 14)).status == "blocked"


def test_zero_rows_for_a_past_day_blocks(
    state: RepoState, conn: psycopg.Connection[Any], serve: Callable[[Handler], None]
) -> None:
    m = isolated(state, "Forecasts_Hour")
    ensure_refs(conn, state, m)
    serve(fixture_response("Forecasts_Hour__empty"))
    out = ingest.run_ingest(state, m, date(2025, 6, 1))
    assert out.status == "blocked"
    assert any(d.startswith("hard:content:zero rows") for d in out.deviations)
    assert len(beads(conn, m)) == 1


def test_transport_failure_is_recorded_with_one_bead(
    state: RepoState, conn: psycopg.Connection[Any], serve: Callable[[Handler], None]
) -> None:
    """Bug e: no response at all still leaves a failed run and one bead."""
    m = isolated(state, "ImbalancePrice")
    ensure_refs(conn, state, m)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    serve(refuse)
    with pytest.raises(httpx.ConnectError):
        ingest.run_ingest(state, m, date(2026, 10, 14))
    ((run_id,),) = conn.execute("select run_id from ops.ingest_run where source = %s", (m.source,)).fetchall()
    run = run_row(conn, str(run_id))
    assert run["status"] == "failed" and run["raw_uris"] == []
    assert "hard:transport:ConnectError" in run["error"]
    ((title, _body, raw_uri),) = beads(conn, m)
    assert "fetch failed" in title and raw_uri is None
    assert observations(conn, str(run_id)) == 0


def test_parse_error_files_one_bead_and_writes_nothing(
    state: RepoState, conn: psycopg.Connection[Any], serve: Callable[[Handler], None]
) -> None:
    m = isolated(state, "DayAheadPrices", zones={"DK1": "dk.zone.dk1", "DK2": "dk.zone.dk2"})
    ensure_refs(conn, state, m)
    serve(fixture_response("DayAheadPrices__dst_end", lambda r: r.update(DayAheadPriceEUR=str(r["DayAheadPriceEUR"]))))
    with pytest.raises(ParseError):
        ingest.run_ingest(state, m, date(2025, 10, 26))
    assert conn.execute("select status from ops.ingest_run where source = %s", (m.source,)).fetchall() == [("failed",)]
    assert len(beads(conn, m)) == 1
    assert conn.execute(
        "select count(*) from core.series where series_key like %s", (f"%.{m.source_short}.%",)
    ).fetchone() == (0,)


def test_recent_ticks_skip_old_revisions_the_daily_full_run_captures(
    state: RepoState, conn: psycopg.Connection[Any], serve: Callable[[Handler], None]
) -> None:
    """A value revised three days later is missed by a frequent tick (recent_lookback) and captured by the
    run with the full lookback, as a new version; unchanged values are not rewritten."""
    zones = {"DK1": "dk.zone.dk1", "DK2": "dk.zone.dk2"}
    m = isolated(
        state,
        "DayAheadPrices",
        zones=zones,
        lookback="P3D",
        lookahead="P0D",
        recent_lookback="PT3H",
        history_end_local=date(2026, 2, 18),  # later partitions expect no rows: the fixture ends there
    )
    ensure_refs(conn, state, m)
    day = date(2026, 2, 20)
    tick = datetime(2026, 2, 20, 12, 20, tzinfo=UTC)
    revised = ("2026-02-17T12:30:00", "DK1")

    def revise(rec: dict[str, Any]) -> None:
        if (rec["TimeUTC"], rec["PriceArea"]) == revised:
            rec["DayAheadPriceEUR"] = 80.0

    serve(fixture_response("DayAheadPrices__spike"))
    first = ingest.run_ingest(state, m, day)
    assert first.status == "ok"
    assert first.rows_written == 2 * 96 * 2  # 02-17 and 02-18, both zones
    serve(fixture_response("DayAheadPrices__spike", revise))
    quick = ingest.run_ingest(state, m, day, recent_at=tick)
    assert (quick.status, quick.rows_written) == ("ok", 0)
    full = ingest.run_ingest(state, m, day)
    assert (full.status, full.rows_written) == ("ok", 1)
