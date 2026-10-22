"""Review of quarantined runs, offline (scanner.runtime.quarantine; ADR 0013, docs/quality.md layers 2, 3).

Synthetic values. The SQL side (core.observation_effective, decisions, evidence queries) is covered by
tests/test_quarantine_db.py.
"""

from __future__ import annotations

from datetime import UTC, datetime

import polars as pl
import pytest

from scanner import cli
from scanner.features.inputs import OBS_SCHEMA, review_counts
from scanner.runtime.quarantine import (
    ClassEvidence,
    QuarantinedRun,
    Reference,
    classify,
    format_summary,
    plan_accepts,
    validate_decision,
)

T0 = datetime(2026, 2, 17, tzinfo=UTC)


def ref(value: float | None, status: str = "ok", key: str = "ref") -> Reference:
    return Reference(key, value, status)


def test_classify_buckets() -> None:
    bucket, diff = classify(911.140015, [ref(911.14)], 0.01)  # EDS publishes float32 artefacts
    assert bucket == "matched" and diff == pytest.approx(1.5e-5)
    assert classify(-480.01, [ref(-480.01, "unverified")], 0.01)[0] == "matched_unverified"
    assert classify(50.0, [ref(50.0, "unverified"), ref(50.0)], 0.01)[0] == "matched"
    # every reference must agree: one ok match does not outweigh a disagreeing reference
    assert classify(50.0, [ref(50.005), ref(50.02, "unverified")], 0.01) == ("mismatched", pytest.approx(0.02))
    assert classify(50.0, [], 0.01) == ("uncovered", None)
    assert classify(None, [ref(50.0)], 0.01) == ("uncovered", None)  # an explicit null is never "matched"
    assert classify(50.0, [ref(None)], 0.01) == ("uncovered", None)


def test_tolerance_boundary() -> None:
    assert classify(42.26, [ref(42.25)], 0.01)[0] == "matched"  # exactly the tolerance, despite float noise
    assert classify(42.26, [ref(42.2499)], 0.01)[0] == "mismatched"


def evidence(*buckets: str, cls: str = "day_ahead_price", tolerance: float | None = 0.01) -> ClassEvidence:
    ev = ClassEvidence(cls, "EUR/MWh", tolerance)
    refs = {
        "matched": [ref(1.0, key="a")],
        "matched_unverified": [ref(1.0, "unverified", key="b")],
        "mismatched": [ref(2.0, key="a")],
        "uncovered": [],
    }
    for b in buckets:
        ev.add(1.0, refs[b])
    return ev


def test_class_evidence_status() -> None:
    ev = evidence("matched", "matched")
    assert (ev.status, ev.values, ev.max_abs_diff, ev.references) == ("corroborated", 2, 0.0, {"a"})
    ev.add(5.0, [])
    assert ev.status == "not corroborated" and ev.counts["uncovered"] == 1
    assert evidence("matched", "mismatched").max_abs_diff == 1.0
    assert evidence("matched_unverified").status == "not corroborated"
    assert evidence("uncovered", "uncovered").status == "no independent source"
    # a class without a tolerance is never compared, even if another series exists
    assert evidence("matched", cls="balancing.mfrr_need", tolerance=None).status == "no independent source"


def qrun(
    run_id: str, *ev: ClassEvidence, decision: str | None = None, dataset: str = "DayAheadPrices"
) -> QuarantinedRun:
    run = QuarantinedRun(run_id, "energidataservice", dataset, "2026-02-17", T0, [], 0, 0, decision, evidence=list(ev))
    run.rows = sum(e.values for e in ev)
    return run


def test_run_outcome_and_accept_plan() -> None:
    corroborated = evidence("matched")
    runs = [
        qrun("a", corroborated),
        qrun("b", corroborated, decision="reject"),  # a person's reject is never overridden
        qrun("c", corroborated, decision="accept"),  # already accepted
        qrun("d", corroborated, evidence("uncovered", cls="balancing.mfrr_need", tolerance=None)),
        qrun("e", evidence("matched_unverified")),
        qrun("f", evidence("uncovered", cls="imbalance_price.nordic_single", tolerance=None), dataset="ImbalancePrice"),
        qrun("g"),
    ]
    outcomes = {r.run_id: r.outcome for r in runs}
    assert outcomes == {
        "a": "corroborated",
        "b": "corroborated",
        "c": "corroborated",
        "d": "not corroborated",
        "e": "not corroborated",
        "f": "no independent source",
        "g": "no current rows",
    }
    assert [r.run_id for r in plan_accepts(runs)] == ["a"]
    summary = format_summary(runs).splitlines()
    assert summary[1].split() == ["energidataservice.DayAheadPrices", "6", "4", "3", "0", "2", "6"]
    assert summary[2].split() == ["energidataservice.ImbalancePrice", "1", "1", "0", "1", "0", "1"]


@pytest.mark.parametrize(
    ("decision", "by", "reason"),
    [("approve", "Ana", "spike confirmed"), ("accept", " ", "spike confirmed"), ("reject", "Ana", "")],
)
def test_validate_decision_refuses(decision: str, by: str, reason: str) -> None:
    with pytest.raises(ValueError):
        validate_decision(decision, by, reason)


def test_validate_decision_accepts() -> None:
    validate_decision("reject", "Ana", "DKK values in the EUR field")


def test_quarantine_command_arguments() -> None:
    parse = cli.build_parser().parse_args
    a = parse(["quarantine", "list", "--dataset", "DayAheadPrices"])
    assert (a.action, a.dataset, a.handler) == ("list", "DayAheadPrices", cli.quarantine_cmd)
    a = parse(["quarantine", "decide", "--run-id", "x", "--decision", "reject", "--by", "Ana", "--reason", "r"])
    assert (a.run_id, a.decision, a.by, a.reason, a.yes) == ("x", "reject", "Ana", "r", False)
    assert parse(["quarantine", "accept-corroborated", "--by", "Ana", "--reason", "r", "--yes"]).yes is True
    for bad in (["quarantine"], ["quarantine", "approve"], ["quarantine", "decide", "--decision", "maybe"]):
        with pytest.raises(SystemExit):
            parse(bad)


@pytest.mark.parametrize(
    "argv",
    [
        ["decide", "--decision", "accept", "--by", "Ana", "--reason", "r"],
        ["decide", "--run-id", "not-a-uuid", "--decision", "accept", "--by", "Ana", "--reason", "r"],
        ["decide", "--run-id", "4624f2b9-4044-4982-93a6-c47316a60600", "--decision", "accept", "--reason", "r"],
        ["accept-corroborated", "--by", "Ana"],
    ],
)
def test_bad_arguments_exit_2_before_connecting(
    argv: list[str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The database URL points at a closed port: reaching it would raise instead of returning 2."""
    from scanner.settings import get_settings

    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none?connect_timeout=1")
    get_settings.cache_clear()
    try:
        assert cli.main(["quarantine", *argv]) == 2
    finally:
        get_settings.cache_clear()
    assert capsys.readouterr().err


def test_review_counts() -> None:
    rows = [("r1", "ok"), ("r2", "unverified"), ("r2", "unverified"), ("r3", "unverified"), ("r4", "verified")]
    df = pl.DataFrame(
        {"run_id": [r for r, _ in rows], "review_status": [s for _, s in rows]},
        schema={"run_id": OBS_SCHEMA["run_id"], "review_status": OBS_SCHEMA["review_status"]},
    )
    assert review_counts([df, df.head(1)]) == {
        "unverified_values": 3,
        "verified_values": 1,
        "unverified_ingest_runs": ["r2", "r3"],
    }
    assert review_counts([]) == {"unverified_values": 0, "verified_values": 0, "unverified_ingest_runs": []}
