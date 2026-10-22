from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import date
from types import SimpleNamespace

import pytest

from scanner import REPO_ROOT, cli
from scanner.core.registry import load_scenarios
from scanner.runtime import ingest
from scanner.runtime.context import RepoState
from scanner.runtime.ingest import IngestOutcome
from scanner.runtime.migrate import up_section

OFFICIAL = next(s.id for s in load_scenarios(REPO_ROOT).values() if s.status == "official")


def manifest(state: RepoState, dataset: str):  # type: ignore[no-untyped-def]
    return next(m for m in state.manifests if m.dataset == dataset)


def test_commands_and_arguments() -> None:
    parse = cli.build_parser().parse_args
    assert set(cli._COMMANDS) >= {"migrate", "load-curated", "ingest", "snapshot", "export"}
    a = parse(["ingest", "--dataset", "all", "--from", "2025-03-04"])
    assert (a.dataset, a.start, a.end, a.workers) == ("all", date(2025, 3, 4), None, 1)
    a = parse(["ingest", "--dataset", "DayAheadPrices", "--from", "2025-10-01", "--to", "2025-10-02", "--workers", "3"])
    assert (a.end, a.workers, a.handler) == (date(2025, 10, 2), 3, cli.ingest_cmd)
    assert parse(["load-curated", "--dry-run"]).dry_run is True
    assert parse(["load-curated"]).dry_run is False
    assert parse(["snapshot"]).id == "all"
    assert parse(["snapshot", "--id", "dk.eds.meta.DayAheadPrices"]).id == "dk.eds.meta.DayAheadPrices"
    assert parse(["migrate"]).handler is cli.migrate_cmd
    assert parse(["export"]).handler is cli.export_cmd
    a = parse(["raw-uris", "migrate"])
    assert (a.handler, a.yes, a.verify) == (cli.raw_uris_cmd, False, False)  # a dry run by default
    assert parse(["raw-uris", "migrate", "--yes", "--verify"]).yes is True
    for bad in (["ingest", "--dataset", "all"], ["ingest", "--dataset", "all", "--from", "04.03.2025"], []):
        with pytest.raises(SystemExit):
            parse(bad)


def test_ingest_days_are_clipped_to_history(state: RepoState) -> None:
    dap, spot = manifest(state, "DayAheadPrices"), manifest(state, "Elspotprices")
    today = date(2025, 10, 3)
    assert cli.ingest_days(dap, date(2025, 9, 29), None, today) == [date(2025, 10, d) for d in (1, 2, 3)]
    assert cli.ingest_days(spot, date(2025, 9, 29), date(2025, 10, 2), today) == [date(2025, 9, 29), date(2025, 9, 30)]
    assert cli.ingest_days(dap, date(2025, 10, 2), date(2025, 12, 1), today) == [date(2025, 10, 2), today]
    assert cli.ingest_days(dap, date(2025, 10, 3), date(2025, 10, 2), today) == []


def test_select_manifests(state: RepoState) -> None:
    every = cli.select_manifests(state.manifests, "all")
    assert {m.dataset for m in every} >= {"DayAheadPrices", "Elspotprices", "ImbalancePrice", "Forecasts_Hour"}
    assert all(m.status in ("active", "backfill_only") for m in every)
    assert [m.dataset for m in cli.select_manifests(state.manifests, "dk_eds_imbalanceprice")] == ["ImbalancePrice"]
    with pytest.raises(ValueError, match="choose from"):
        cli.select_manifests(state.manifests, "NoSuchDataset")


def test_ingest_continues_past_failed_days_and_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[str, date, str]] = []

    def fake_run_ingest(state: RepoState, m, day: date) -> IngestOutcome:  # type: ignore[no-untyped-def]
        calls.append((m.dataset, day, threading.current_thread().name))
        if day == date(2025, 9, 29):
            raise RuntimeError("source down")
        return IngestOutcome(run_id="r", status="ok", rows_written=24)

    monkeypatch.setattr(ingest, "run_ingest", fake_run_ingest)
    code = cli.main(["ingest", "--dataset", "Elspotprices", "--from", "2025-09-28", "--to", "2025-10-05"])
    assert code == 1
    assert [d for _, d, _ in calls] == [date(2025, 9, 28), date(2025, 9, 29), date(2025, 9, 30)]
    out = capsys.readouterr().out
    assert "RuntimeError: source down" in out
    assert "1 day(s) not ok: energidataservice.Elspotprices 2025-09-29" in out


def test_ingest_parallel_datasets_keep_days_sequential(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, date, str]] = []

    def fake_run_ingest(state: RepoState, m, day: date) -> IngestOutcome:  # type: ignore[no-untyped-def]
        calls.append((m.dataset, day, threading.current_thread().name))
        return IngestOutcome(run_id="r", status="ok", rows_written=1)

    monkeypatch.setattr(ingest, "run_ingest", fake_run_ingest)
    assert cli.main(["ingest", "--dataset", "all", "--from", "2025-09-29", "--to", "2025-10-02", "--workers", "4"]) == 0
    for dataset in {d for d, _, _ in calls}:
        mine = [(day, thread) for d, day, thread in calls if d == dataset]
        assert [day for day, _ in mine] == sorted(day for day, _ in mine)
        assert len({thread for _, thread in mine}) == 1


def _outcome(name: str, status: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, run_id=f"run-{name}", status=status, rows_written=1, notes=[])


def test_features_and_pipeline_commands() -> None:
    parse = cli.build_parser().parse_args
    a = parse(["features", "--end-day", "2026-10-18"])
    assert (a.handler, a.country, a.end_day) == (cli.features_cmd, "dk", date(2026, 10, 18))
    assert parse(["pipeline"]).handler is cli.pipeline_cmd


def test_runs_step_status() -> None:
    def status(*statuses: str) -> str:
        return cli.runs_step(lambda: [_outcome(f"o{i}", s) for i, s in enumerate(statuses)])()[0]

    assert status("ok", "ok") == "ok"
    assert status("ok", "skipped") == "partial" and status("partial") == "partial"
    assert status("skipped", "skipped") == "failed"


def test_pipeline_stops_at_the_first_failed_step(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []

    def step(name: str, *statuses: str) -> Callable[..., list[SimpleNamespace]]:
        def run(*args: object) -> list[SimpleNamespace]:
            calls.append(name)
            return [_outcome(name, s) for s in statuses]

        return run

    monkeypatch.setattr(cli, "load_curated_step", lambda: (calls.append("load-curated"), ("ok", "0 changes"))[1])
    monkeypatch.setattr(cli, "run_all_features", step("features", "ok", "skipped"))
    monkeypatch.setattr(cli, "run_scores", step("scores", "skipped", "skipped"))
    monkeypatch.setattr(cli, "run_valuation", step("valuation", "ok"))
    assert cli.main(["pipeline"]) == 1
    assert calls == ["load-curated", "features", "scores"]
    lines = capsys.readouterr().out.splitlines()
    summary = lines[lines.index(next(line for line in lines if line.startswith("step"))) + 1 :]
    assert [line.split()[:2] for line in summary] == [
        ["load-curated", "ok"],
        ["features", "partial"],
        [f"scores:{OFFICIAL}", "failed"],  # default: the official scenario
        [f"valuation:{OFFICIAL}", "not"],
        ["export", "not"],
    ]

    calls.clear()
    monkeypatch.setattr(cli, "run_scores", step("scores", "ok"))
    monkeypatch.setattr(cli, "export_all", lambda: calls.append("export") or [])
    assert cli.main(["pipeline", "--all-scenarios"]) == 0
    # features once, then scores and valuation per scenario (official first), then the export
    assert calls == ["load-curated", "features", *["scores", "valuation"] * 5, "export"]
    assert "scores:spec_v0" in capsys.readouterr().out

    def boom(*args: object) -> list[SimpleNamespace]:
        raise RuntimeError("database down")

    monkeypatch.setattr(cli, "run_scores", boom)
    results = cli.run_pipeline([("a", cli.runs_step(lambda: [_outcome("a", "ok")])), ("b", cli.runs_step(boom))])
    assert [(r.step, r.status) for r in results] == [("a", "ok"), ("b", "failed")]
    assert results[1].detail == "RuntimeError: database down"


def test_up_section() -> None:
    sql = "-- migrate:up\ncreate table t ();\n-- migrate:down\ndrop table t;\n"
    assert up_section(sql).strip() == "create table t ();"


def test_curated_asset_is_registered() -> None:
    from dagster import AssetKey

    from scanner.ingestion_definitions import defs

    assert AssetKey(["curated", "files"]) in defs.resolve_asset_graph().get_all_asset_keys()


def test_prune_and_catch_up_arguments() -> None:
    parse = cli.build_parser().parse_args
    a = parse(["prune", "--keep", "14"])
    assert (a.keep, a.yes, a.protect, a.handler) == (14, False, "served", cli.prune_cmd)  # a dry run by default
    assert parse(["prune", "--keep", "3", "--yes"]).yes is True
    with pytest.raises(SystemExit):
        parse(["prune"])
    a = parse(["catch-up", "--since", "2026-10-01"])
    assert (a.dataset, a.since, a.plan, a.handler) == ("all", date(2026, 10, 1), False, cli.catch_up_cmd)


def test_catch_up_ingests_only_the_given_days(state: RepoState, monkeypatch: pytest.MonkeyPatch) -> None:
    from scanner.runtime import catchup

    seen: list[date] = []

    def fake_run_ingest(state: RepoState, m, day: date) -> IngestOutcome:  # type: ignore[no-untyped-def]
        seen.append(day)
        return IngestOutcome(run_id="r", status="ok", rows_written=1)

    monkeypatch.setattr(ingest, "run_ingest", fake_run_ingest)
    m = manifest(state, "DayAheadPrices")
    days = [date(2026, 10, 2), date(2026, 10, 6), date(2026, 10, 7)]
    results = catchup.run_catch_up(state, [catchup.DatasetPlan(m, todo=days)], max_days=2)
    assert seen == days[:2] and [r.status for r in results] == ["ok", "ok"]
    assert "gap (current state only" in catchup.format_plans([], catchup.unrecoverable(state))
