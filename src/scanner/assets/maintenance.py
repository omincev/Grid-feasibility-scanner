"""Weekly retention job of the analytics location: prune old feature runs (scanner.runtime.prune).

The daily compute job appends a full feature run each day (~25k rows). `schedule_prune_feature_runs` deletes
runs beyond the newest KEEP per feature that no served score and no approved scenario's output references.
It connects with DATABASE_URL (the owner role), as the curated load does: scanner_analytics may only insert into
feat and val (migration 20261018130000), and deleting derived rows is not something the compute path needs.
"""

from dagster import OpExecutionContext, ScheduleDefinition, job, op

from scanner.assets.automation import default_status
from scanner.runtime.prune import prune_feature_runs
from scanner.settings import get_settings

# Two weeks of daily runs plus slack for a failed day; the runs scores point at are kept regardless.
KEEP = 14
# After the Sunday-night compute and exports; before the Monday capacity-map capture.
PRUNE_CRON = "0 4 * * 0"


@op
def prune_feature_runs_op(context: OpExecutionContext) -> None:
    context.log.info(prune_feature_runs(get_settings().database_url, KEEP, yes=True))


@job(name="job_prune_feature_runs", description=f"Delete feature runs beyond the newest {KEEP} per feature.")
def prune_job() -> None:
    prune_feature_runs_op()


schedule_prune_feature_runs = ScheduleDefinition(
    name="schedule_prune_feature_runs",
    job=prune_job,
    cron_schedule=PRUNE_CRON,
    execution_timezone="Europe/Copenhagen",
    default_status=default_status(),
)

MAINTENANCE_JOBS = [prune_job]
MAINTENANCE_SCHEDULES = [schedule_prune_feature_runs]
