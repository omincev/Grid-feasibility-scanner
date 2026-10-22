"""Review of quarantined runs against a migrated database. Run with: TEST_DATABASE_URL=... pytest -m db

Covers core.observation_effective and its as-of function (through load_input and run_features), the
corroboration evidence, decisions, and accept-corroborated with and without --yes. Test rows use datasets
named QTest*, codebook entries of those datasets, series keys starting with test.quarantine. and a test-only
zone (so series the pipeline wrote for real zones are neither inputs nor references); they are removed
afterwards. Never point TEST_DATABASE_URL at the shared integration database.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from conftest import zones_root

from scanner import cli
from scanner.core import db
from scanner.features.inputs import load_input, review_counts
from scanner.features.registry import InputSpec
from scanner.features.run import run_features
from scanner.runtime import quarantine
from scanner.settings import get_settings

pytestmark = pytest.mark.db

PREFIX = "test.quarantine."
DK1 = "dk.zone.test_quarantine"  # stands in for DK1
Q = timedelta(minutes=15)
DAY1 = datetime(2026, 2, 16, 23, tzinfo=UTC)  # 2026-02-17 00:00 Copenhagen
DAY2, DAY3 = DAY1 + timedelta(days=1), DAY1 + timedelta(days=2)

PRICE = ("day_ahead_price", "EUR/MWh")
# entry_id: (source, dataset, class, unit)
ENTRIES = {
    "dk.energidataservice.QTestPrices.PriceEUR": ("energidataservice", "QTestPrices", *PRICE),
    "dk.entsoe.QTestPrices.PriceEUR": ("entsoe", "QTestPrices", *PRICE),
    "dk.energidataservice.QTestImbalance.SpotPriceEUR": ("energidataservice", "QTestImbalance", *PRICE),
    "dk.energidataservice.QTestImbalance.Need": ("energidataservice", "QTestImbalance", "balancing.mfrr_need", "MW"),
}
EDS, ENTSOE, SPOT, NEED = ENTRIES
DAY_AHEAD = "dk.eds.DayAheadPrices.DayAheadPriceEUR"  # the entry F1 reads


@pytest.fixture()
def conn(monkeypatch: pytest.MonkeyPatch) -> Iterator[psycopg.Connection[Any]]:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("GIT_SHA", "testsha")
    get_settings.cache_clear()
    with psycopg.connect(url) as c:
        _cleanup(c)
        _reference(c)
        c.commit()
        try:
            yield c
        finally:
            c.rollback()
            _cleanup(c)
            get_settings.cache_clear()


def _cleanup(c: psycopg.Connection[Any]) -> None:
    runs = "select run_id from ops.ingest_run where dataset like 'QTest%%'"
    computed = "select run_id from ops.compute_run where params->>'test' = 'quarantine'"
    c.execute(f"delete from feat.feature_value where run_id in ({computed}) or entity_id = %s", (DK1,))
    c.execute(f"delete from ops.compute_run where run_id in ({computed})")
    c.execute(f"delete from ops.quarantine_decision where run_id in ({runs})")
    c.execute(f"delete from ops.fingerprint where run_id in ({runs})")
    series = "select series_id from core.series where series_key like %s"
    c.execute(f"delete from core.observation where series_id in ({series})", (f"{PREFIX}%",))
    c.execute("delete from core.series where series_key like %s", (f"{PREFIX}%",))
    c.execute("delete from ops.ingest_run where dataset like 'QTest%%'")
    c.execute("delete from ref.codebook_entry where dataset like 'QTest%%'")
    c.execute("delete from ref.entity where entity_id = %s", (DK1,))
    c.commit()


def _reference(c: psycopg.Connection[Any]) -> None:
    c.execute(
        "insert into ref.country (code, name, status, timezone, currency, config, source_path, content_hash) "
        "values ('dk','Denmark','active','Europe/Copenhagen','DKK','{}','t','t') on conflict do nothing"
    )
    c.execute(
        "insert into ref.entity (entity_id, kind, country_code, name) values (%s, 'zone', 'dk', 'DK1') "
        "on conflict do nothing",
        (DK1,),
    )
    entries = {**ENTRIES, DAY_AHEAD: ("eds", "DayAheadPrices", "day_ahead_price", "EUR/MWh")}
    for entry_id, (source, dataset, cls, unit) in entries.items():
        c.execute("insert into ref.equivalence_class values (%s, 'x', 'x') on conflict do nothing", (cls,))
        c.execute(
            "insert into ref.codebook_entry (entry_id, source, dataset, country_code, field, equivalence_class, "
            "kind, unit, resample, definition, status, source_path, content_hash) "
            "values (%s, %s, %s, 'dk', %s, %s, 'actual', %s, 'mean', '{}', 'draft', 't', 't') on conflict do nothing",
            (entry_id, source, dataset, entry_id.rsplit(".", 1)[1], cls, unit),
        )


def _run(c: psycopg.Connection[Any], entry_id: str, *, quarantined: bool) -> uuid.UUID:
    """An ingest run of the entry's dataset; a soft fingerprint deviation if quarantined."""
    source, dataset, *_ = ENTRIES.get(entry_id, ("energidataservice", "QTestDayAhead"))
    run_id = uuid.uuid4()
    c.execute(
        "insert into ops.ingest_run (run_id, source, dataset, partition_key, status) values (%s, %s, %s, 'p', 'ok')",
        (run_id, source, dataset),
    )
    devs = [{"severity": "soft", "level": "content", "detail": "value range outside baseline"}] if quarantined else []
    c.execute(
        "insert into ops.fingerprint (run_id, payload, deviations, severity) values (%s, '{}', %s, %s)",
        (run_id, json.dumps(devs), "soft" if quarantined else "none"),
    )
    c.commit()
    return run_id


def _write(
    c: psycopg.Connection[Any], entry_id: str, start: datetime, values: list[float | None], *, quarantined: bool
) -> uuid.UUID:
    """Rows of one series (DK1) written by a new ingest run; returns the run ID."""
    unit = ENTRIES[entry_id][3] if entry_id in ENTRIES else "EUR/MWh"
    key = f"{PREFIX}{entry_id}"
    sid = db.ensure_series(c, db.SeriesSpec(key, entry_id, DK1, unit, Q, "actual", "public", None))
    run_id = _run(c, entry_id, quarantined=quarantined)
    quality = "quarantined" if quarantined else "ok"
    rows = [db.ObservationRow(key, start + i * Q, start + (i + 1) * Q, None, v, quality) for i, v in enumerate(values)]
    db.write_observations(c, rows, {key: sid}, run_id)
    c.commit()
    return run_id


def _decide(c: psycopg.Connection[Any], run_id: uuid.UUID, decision: str) -> datetime:
    c.execute("select pg_sleep(0.01)")
    c.commit()
    decision_id = quarantine.record_decision(c, run_id, decision, "Test Reviewer", f"test {decision}")
    row = c.execute("select decided_at from ops.quarantine_decision where decision_id = %s", (decision_id,)).fetchone()
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


def test_quarantined_rows_are_used_until_rejected(conn: psycopg.Connection[Any]) -> None:
    _write(conn, EDS, DAY1, [10.0, 20.0], quarantined=False)
    rq = _write(conn, EDS, DAY1 + 2 * Q, [911.14, -480.01], quarantined=True)
    spec = InputSpec("day_ahead_price", (EDS,))

    def load() -> Any:
        return load_input(conn, ("day_ahead_price",), spec, [DK1], DAY1, DAY2)

    df = load()
    assert df["value"].to_list() == [10.0, 20.0, 911.14, -480.01]
    assert df["review_status"].to_list() == ["ok", "ok", "unverified", "unverified"]
    assert review_counts([df]) == {"unverified_values": 2, "verified_values": 0, "unverified_ingest_runs": [str(rq)]}

    accepted_at = _decide(conn, rq, "accept")
    df = load()
    assert df["review_status"].to_list() == ["ok", "ok", "verified", "verified"]
    assert review_counts([df])["verified_values"] == 2

    rejected_at = _decide(conn, rq, "reject")
    assert load()["value"].to_list() == [10.0, 20.0]

    # Append-only: the stored rows keep their quality; decisions decide what readers see.
    stored = conn.execute("select count(*) from core.observation where run_id = %s and quality = 'quarantined'", (rq,))
    assert stored.fetchone() == (2,)

    def asof(t: datetime) -> list[tuple[Any, ...]]:
        return conn.execute(
            "select e.review_status, e.usable from core.observation_effective_asof(%s) e "
            "join core.series s on s.series_id = e.series_id where s.series_key = %s and e.run_id = %s",
            (t, f"{PREFIX}{EDS}", rq),
        ).fetchall()

    assert asof(accepted_at - timedelta(microseconds=1)) == [("unverified", True)] * 2
    assert asof(accepted_at) == [("verified", True)] * 2
    assert asof(rejected_at) == [("rejected", False)] * 2


def test_feature_runs_record_unverified_inputs(conn: psycopg.Connection[Any], tmp_path: Path) -> None:
    run_id = _write(conn, DAY_AHEAD, DAY1, [50.0 + i for i in range(96)], quarantined=True)
    root = zones_root(tmp_path, [DK1])

    def f1() -> tuple[str, dict[str, Any]]:
        url = os.environ["TEST_DATABASE_URL"]
        (out,) = run_features(["f1_price_spread"], database_url=url, end_day=date(2026, 2, 18), root=root)
        row = conn.execute(
            'update ops.compute_run set params = params || \'{"test": "quarantine"}\' where run_id = %s '
            "returning params",
            (uuid.UUID(out.run_id),),
        ).fetchone()
        conn.commit()
        assert row is not None
        return out.status, row[0]

    status, params = f1()
    assert status in ("ok", "partial")
    assert (params["unverified_values"], params["unverified_ingest_runs"]) == (96, [str(run_id)])
    assert params["window"]  # the params stored when the run opened are kept

    _decide(conn, run_id, "reject")
    status, params = f1()
    assert status == "skipped" and params["unverified_values"] == 0


def _evidence_runs(c: psycopg.Connection[Any]) -> dict[str, uuid.UUID]:
    """A corroborated by ENTSO-E within tolerance; B one value 0.5 off; C a class without a tolerance;
    D and F matching each other on day 3 while both are unverified."""
    runs = {
        "A": _write(c, EDS, DAY1, [911.140015, -480.01, 50.0, 60.0], quarantined=True),
        "E": _write(c, ENTSOE, DAY1, [911.14, -480.01, 50.004, 60.0], quarantined=False),
        "C": _write(c, NEED, DAY1, [1.0, 2.0], quarantined=True),
        "B": _write(c, EDS, DAY2, [70.0, 80.0], quarantined=True),
        "E2": _write(c, ENTSOE, DAY2, [70.0, 80.5], quarantined=False),
        "D": _write(c, SPOT, DAY3, [30.0, 40.0], quarantined=True),
        "F": _write(c, ENTSOE, DAY3, [30.001, 40.0], quarantined=True),
    }
    return runs


def _listed(c: psycopg.Connection[Any]) -> dict[str, quarantine.QuarantinedRun]:
    return {r.run_id: r for r in quarantine.quarantined_runs(c) if r.dataset.startswith("QTest")}


def test_corroboration_evidence(conn: psycopg.Connection[Any]) -> None:
    runs = _evidence_runs(conn)
    listed = _listed(conn)
    assert set(listed) == {str(runs[k]) for k in "ABCDF"}
    a, b, c, d, f = (listed[str(runs[k])] for k in "ABCDF")
    assert (a.rows, a.superseded, a.deviations) == (4, 0, ["soft:content:value range outside baseline"])
    assert a.span == (DAY1, DAY1 + 4 * Q)
    (ev,) = a.evidence
    assert (a.outcome, ev.status, ev.counts["matched"]) == ("corroborated", "corroborated", 4)
    assert ev.max_abs_diff == pytest.approx(0.004) and ev.references == {f"{PREFIX}{ENTSOE}"}
    (ev,) = b.evidence
    assert (b.outcome, ev.counts["matched"], ev.counts["mismatched"]) == ("not corroborated", 1, 1)
    assert ev.max_abs_diff == pytest.approx(0.5)
    assert c.outcome == "no independent source" and c.evidence[0].tolerance is None
    for run in (d, f):  # each is the other's only reference, and neither is reviewed
        assert (run.outcome, run.evidence[0].counts["matched_unverified"]) == ("not corroborated", 2)

    text = quarantine.format_runs(list(listed.values()))
    assert "max |diff| 0.5 EUR/MWh (tolerance 0.01)" in text
    assert f"run {runs['A']}  quarantined rows 4 over [2026-02-16T23:00Z, 2026-02-17T00:00Z)" in text
    assert "balancing.mfrr_need: no independent source (class not reconciled" in text

    # Once a person accepts F, D is corroborated by a verified value.
    _decide(conn, runs["F"], "accept")
    listed = _listed(conn)
    assert listed[str(runs["D"])].outcome == "corroborated"
    assert listed[str(runs["F"])].decision == "accept"


def _decisions(c: psycopg.Connection[Any]) -> list[tuple[Any, ...]]:
    return c.execute(
        "select d.run_id, d.decision, d.decided_by from ops.quarantine_decision d "
        "join ops.ingest_run r on r.run_id = d.run_id where r.dataset like 'QTest%%' order by d.decision_id"
    ).fetchall()


def test_accept_corroborated_writes_only_with_yes(
    conn: psycopg.Connection[Any], capsys: pytest.CaptureFixture[str]
) -> None:
    runs = _evidence_runs(conn)
    for dataset in ("QTestPrices", "QTestImbalance"):
        assert cli.main(["quarantine", "accept-corroborated", "--dataset", dataset, "--by", "A", "--reason", "r"]) == 0
    out = capsys.readouterr().out
    assert f"would accept energidataservice.QTestPrices p run {runs['A']}" in out and "add --yes" in out
    assert _decisions(conn) == []

    plan, ids = quarantine.accept_corroborated(conn, decided_by="Ana", reason="r", dataset="QTestPrices")
    assert [r.run_id for r in plan] == [str(runs["A"])] and ids == []
    assert _decisions(conn) == []

    argv = ["quarantine", "accept-corroborated", "--dataset", "QTestPrices", "--by", "Ana", "--reason", "matches"]
    assert cli.main([*argv, "--yes"]) == 0
    assert _decisions(conn) == [(runs["A"], "accept", "Ana")]
    assert cli.main([*argv, "--yes"]) == 0  # nothing left to accept
    assert len(_decisions(conn)) == 1

    assert cli.main(["quarantine", "list", "--dataset", "QTestPrices"]) == 0
    listing = capsys.readouterr().out
    assert "decision: accept by Ana" in listing and "energidataservice.QTestPrices" in listing


def test_decisions_need_a_quarantined_run_and_a_name(conn: psycopg.Connection[Any]) -> None:
    runs = _evidence_runs(conn)
    with pytest.raises(ValueError, match="no quarantined rows"):
        quarantine.record_decision(conn, runs["E"], "accept", "Ana", "r")
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "insert into ops.quarantine_decision (run_id, decided_by, decision, reason) "
            "values (%s, ' ', 'accept', 'r')",
            (runs["A"],),
        )
    conn.rollback()
    argv = ["quarantine", "decide", "--run-id", str(runs["B"]), "--decision", "reject", "--by", "Ana", "--reason", "x"]
    assert cli.main(argv) == 0
    assert _decisions(conn) == [(runs["B"], "reject", "Ana")]
