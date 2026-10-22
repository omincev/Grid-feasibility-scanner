"""The daily compute job of the analytics location (docs/architecture.md, "How data stays current").

`job_compute` runs the groups in COMPUTE_GROUPS in one run, in asset-dependency order: the curated load
(scanner.assets.assumptions), features once, then scores, opportunity types and valuation for every scenario
partition, then the static export. Every scenario runs in that same run because the schedule requests the
whole `scenario` partition range: unpartitioned assets execute once and each scenario-partitioned asset once
over all its partitions (BackfillPolicy.single_run). Launching it by hand: the UI's "Materialize" on the job
with all partitions selected, or `dagster job launch -w workspace.yaml -l analytics -j job_compute
--partition-range <first>...<last>` (partition order as the UI lists them).

It is a schedule rather than declarative automation because the compute assets read every partition of their
ingest inputs: `eager()` would recompute after each of the dozens of ingest runs a day, and `on_cron()` waits for
all deps, including the retired Elspotprices asset and the weekly capacity map, which never or rarely update.
The weekly capacity map reaches scores through F7 in the next daily run (Monday 06:30 capture, 15:30 compute).
Assumption-file changes recompute in the same daily run (scanner.assets.assumptions explains why not sooner).
"""

from collections.abc import Sequence

from dagster import (
    AssetSelection,
    RunRequest,
    ScheduleEvaluationContext,
    SkipReason,
    define_asset_job,
    schedule,
)
from dagster._core.storage.tags import ASSET_PARTITION_RANGE_END_TAG, ASSET_PARTITION_RANGE_START_TAG

from scanner.assets.assumptions import SCENARIO_PARTITIONS
from scanner.assets.automation import default_status

# Phase groups (features_phase3, scores_phase3, valuation_phase6, ...) hold NotBuiltYet assets and stay out;
# "assumptions" contributes only the curated load (asset selections by group leave out source assets).
COMPUTE_GROUPS = ("assumptions", "features", "scores", "valuation", "serving")
# After the day-ahead ingests (DayAheadPrices 13:15, 13:45, 14:30 Copenhagen; ENTSO-E prices 13:30 and
# 14:30 Brussels) have had time to finish behind the per-source run queue.
COMPUTE_CRON = "30 15 * * *"
COMPUTE_TIMEZONE = "Europe/Copenhagen"

compute_job = define_asset_job(
    "job_compute",
    selection=AssetSelection.groups(*COMPUTE_GROUPS),
    description="Curated load, features, then scores, opportunity types and valuation for every scenario, and "
    "the static export, over the latest ingested data.",
)


def every_scenario(partition_keys: Sequence[str]) -> dict[str, str]:
    """Run tags that make one run cover every scenario partition (in the order Dagster stores them)."""
    return {ASSET_PARTITION_RANGE_START_TAG: partition_keys[0], ASSET_PARTITION_RANGE_END_TAG: partition_keys[-1]}


@schedule(
    name="schedule_compute_daily",
    job=compute_job,
    cron_schedule=COMPUTE_CRON,
    execution_timezone=COMPUTE_TIMEZONE,
    default_status=default_status(),
)
def schedule_compute_daily(context: ScheduleEvaluationContext) -> RunRequest | SkipReason:
    keys = context.instance.get_dynamic_partitions(SCENARIO_PARTITIONS.name)
    if not keys:
        return SkipReason("no scenario partitions yet; sensor_assumption_files adds one per scenario file")
    return RunRequest(tags=every_scenario(keys))


COMPUTE_SCHEDULES = [schedule_compute_daily]
