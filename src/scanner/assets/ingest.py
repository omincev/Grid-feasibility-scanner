"""One partitioned ingest asset per manifest, plus schedules (docs/architecture.md).

Each asset's description and metadata show its normalisation rules (scanner.assets.rules).
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from dagster import (
    AssetCheckResult,
    AssetCheckSeverity,
    AssetCheckSpec,
    AssetExecutionContext,
    AssetsDefinition,
    DailyPartitionsDefinition,
    MaterializeResult,
    RunRequest,
    ScheduleDefinition,
    ScheduleEvaluationContext,
    asset,
    define_asset_job,
    schedule,
)

from scanner.assets.automation import default_status
from scanner.assets.keys import ingest_key, ingested
from scanner.assets.rules import ingest_rules
from scanner.core.manifest import Manifest
from scanner.runtime.context import RepoState, repo_state
from scanner.runtime.ingest import run_ingest


def _partitions(m: Manifest, tz: str) -> DailyPartitionsDefinition:
    # Dagster's end_date is exclusive; the manifest's history_end_local is the last day to include.
    end = (m.history_end_local + timedelta(days=1)).isoformat() if m.history_end_local else None
    # DailyPartitionsDefinition end_offset=1 includes today's (incomplete) partition for live sources.
    return DailyPartitionsDefinition(
        start_date=m.history_start_local.isoformat(),
        end_date=end,
        timezone=tz,
        end_offset=0 if end else 1,
    )


# Tag of the runs of a manifest's frequent schedules: fetch only manifest.recent_lookback (scanner.runtime.ingest).
WINDOW_TAG = "ingest_window"
RECENT = "recent"


def run_tags(m: Manifest) -> dict[str, str]:
    """Run tags for ingest runs; infra/dagster.yaml limits concurrency per `source`."""
    return {"source": m.source}


def build_ingest_asset(state: RepoState, m: Manifest) -> AssetsDefinition:
    tz = state.countries[m.country].timezone
    key = ingest_key(m)
    description, metadata = ingest_rules(state, m)

    @asset(
        key=key,
        partitions_def=_partitions(m, tz),
        group_name=f"ingest_{m.source_short}",
        kinds={"python", "postgres"},
        description=description,
        check_specs=[AssetCheckSpec(name="hard_fingerprint", asset=key)],
        metadata=metadata,
        tags=run_tags(m),
    )
    def _ingest(context: AssetExecutionContext):  # type: ignore[no-untyped-def]
        day = date.fromisoformat(context.partition_key)
        recent_at = datetime.now(UTC) if context.run.tags.get(WINDOW_TAG) == RECENT else None
        outcome = run_ingest(state, m, day, recent_at)
        context.log.info(f"run {outcome.run_id}: {outcome.status}, written {outcome.rows_written}")
        yield MaterializeResult(
            metadata={
                "run_id": outcome.run_id,
                "status": outcome.status,
                "rows_received": outcome.rows_received,
                "rows_expected": outcome.rows_expected or -1,
                "rows_written": outcome.rows_written,
                "severity": outcome.severity,
                "deviations": "\n".join(outcome.deviations) or "none",
            }
        )
        yield AssetCheckResult(
            check_name="hard_fingerprint",
            passed=outcome.severity != "hard",
            severity=AssetCheckSeverity.ERROR,
            metadata={"deviations": "\n".join(outcome.deviations) or "none"},
        )

    return _ingest


def build_ingest_defs(
    state: RepoState | None = None,
) -> tuple[list[AssetsDefinition], list[ScheduleDefinition]]:
    state = state or repo_state()
    assets: list[AssetsDefinition] = []
    schedules: list[ScheduleDefinition] = []
    for m in state.manifests:
        if not ingested(m):
            continue
        a = build_ingest_asset(state, m)
        assets.append(a)
        if m.status == "active":
            job = define_asset_job(f"job_{m.asset_name}", selection=[ingest_key(m)], tags=run_tags(m))
            tz = state.countries[m.country].timezone
            tick_tags = {**run_tags(m), WINDOW_TAG: RECENT} if m.recent_lookback else run_tags(m)
            for i, cron in enumerate(m.schedule):
                schedules.append(
                    _day_schedule(f"schedule_{m.asset_name}_{i}", job, cron, m.schedule_timezone, tz, tick_tags)
                )
            schedules.append(
                _day_schedule(f"schedule_{m.asset_name}_yesterday", job, YESTERDAY_CRON, tz, tz, run_tags(m), 1)
            )
    return assets, schedules


# Once a local day has ended: its sources have published the last intervals (production actuals and ENTSO-E
# load and generation up to an hour after the interval), and 01:30 exists exactly once on DST change days.
YESTERDAY_CRON = "30 1 * * *"


def _day_schedule(
    name: str, job: Any, cron: str, exec_tz: str, data_tz: str, tags: dict[str, str], days_back: int = 0
) -> ScheduleDefinition:
    """Each tick materialises the partition of the local day `days_back` days before the tick's own; the manifest
    lookback covers revisions and its lookahead covers data published ahead of its interval.

    The manifest's schedules materialise today's partition (days_back 0), which cannot yet be complete, so its
    runs carry no expected row count. One more schedule per dataset (days_back 1, YESTERDAY_CRON in the data's
    timezone) gives every completed day a run whose fingerprint and row count cover the whole day
    (serving.source_completeness)."""

    @schedule(name=name, job=job, cron_schedule=cron, execution_timezone=exec_tz, default_status=default_status())
    def _s(context: ScheduleEvaluationContext) -> RunRequest:
        t = context.scheduled_execution_time or datetime.now(UTC)
        day = t.astimezone(ZoneInfo(data_tz)).date() - timedelta(days=days_back)
        return RunRequest(partition_key=day.isoformat(), tags=tags)

    return _s
