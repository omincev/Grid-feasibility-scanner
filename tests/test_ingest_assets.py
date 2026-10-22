"""Ingest assets: partitions, run tags, hard fingerprint checks (scanner.assets.ingest)."""

from __future__ import annotations

from datetime import UTC, datetime

from dagster import AssetCheckKey, RunRequest, build_schedule_context

from scanner.assets.ingest import YESTERDAY_CRON, _partitions, build_ingest_defs, ingest_key
from scanner.runtime.context import RepoState


def test_history_end_is_the_last_partition(state: RepoState) -> None:
    m = next(m for m in state.manifests if m.dataset == "Elspotprices")
    assert m.history_end_local is not None
    keys = _partitions(m, "Europe/Copenhagen").get_partition_keys()
    assert keys[0] == "2025-03-04"
    assert keys[-1] == m.history_end_local.isoformat() == "2025-09-30"


def test_live_sources_include_today(state: RepoState) -> None:
    m = next(m for m in state.manifests if m.dataset == "ImbalancePrice")
    now = datetime(2026, 10, 18, 9, tzinfo=UTC)
    keys = _partitions(m, "Europe/Copenhagen").get_partition_keys(current_time=now)
    assert keys[-1] == "2026-10-18"


def test_every_ingest_asset_has_a_hard_fingerprint_check(state: RepoState) -> None:
    assets, _ = build_ingest_defs(state)
    built = {m.dataset: m for m in state.manifests if m.status not in ("draft", "retired")}
    assert len(assets) == len(built) >= 5
    for a in assets:
        (key,) = a.keys
        assert AssetCheckKey(key, "hard_fingerprint") in a.check_keys


def test_ingest_runs_are_tagged_with_their_source(state: RepoState) -> None:
    assets, schedules = build_ingest_defs(state)
    assert schedules
    source_of_job = {f"job_{m.asset_name}": m.source for m in state.manifests}
    source_of_key = {ingest_key(m): m.source for m in state.manifests}
    assert set(source_of_job.values()) >= {"energidataservice", "entsoe"}
    for s in schedules:
        source = source_of_job[s.job.name]  # type: ignore[union-attr]
        assert s.job.tags.get("source") == source  # type: ignore[union-attr]
        ctx = build_schedule_context(scheduled_execution_time=datetime(2026, 10, 18, 11, 15, tzinfo=UTC))
        request = s(ctx)
        assert isinstance(request, RunRequest)
        assert request.tags["source"] == source
        assert request.partition_key == ("2026-10-17" if s.name.endswith("_yesterday") else "2026-10-18")
    for a in assets:
        (key,) = a.keys
        assert a.tags_by_key[key]["source"] == source_of_key[key]
    assert {ingest_key(m) for m in state.manifests if m.status == "active"} <= {k for a in assets for k in a.keys}


def test_every_active_dataset_re_ingests_yesterday_once_the_day_is_complete(state: RepoState) -> None:
    """Runs on the day itself have no expected row count; the yesterday schedule gives every completed day a run
    whose fingerprint and row count cover the whole day (serving.source_completeness)."""
    _, schedules = build_ingest_defs(state)
    by_name = {s.name: s for s in schedules}
    for m in (m for m in state.manifests if m.status == "active"):
        s = by_name[f"schedule_{m.asset_name}_yesterday"]
        tz = state.countries[m.country].timezone
        assert (s.cron_schedule, s.execution_timezone) == (YESTERDAY_CRON, tz)
        # 01:30 local on the day after the autumn DST change (2026-10-25 had 25 hours) and on a winter day
        for tick, day in (("2026-10-26T00:30:00+00:00", "2026-10-25"), ("2026-12-01T00:30:00+00:00", "2026-11-30")):
            request = s(build_schedule_context(scheduled_execution_time=datetime.fromisoformat(tick)))
            assert isinstance(request, RunRequest)
            assert request.partition_key == day and request.tags["source"] == m.source
    assert not any(n.endswith("_yesterday") for n in by_name if "elspotprices" in n)
