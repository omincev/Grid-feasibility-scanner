"""Catch-up job and sensor of the ingestion location (scanner.runtime.catchup).

`sensor_catch_up` looks hourly for local days without a full-day run (ops.ingest_run; a database read only) and
launches `job_catch_up` when there are some and no catch-up run is active; with nothing missing it does nothing.
The job ingests at most MAX_DAYS_PER_RUN days per dataset, so a fresh server fills up over several hourly
rounds, and each day goes through the ordinary run_ingest path under the per-dataset advisory lock, next to the
schedules. Blocked and incomplete days and current-state sources are logged as gaps, not errors.
"""

import psycopg
from dagster import (
    DagsterRunStatus,
    DefaultScheduleStatus,
    DefaultSensorStatus,
    OpExecutionContext,
    RunRequest,
    RunsFilter,
    SensorEvaluationContext,
    SkipReason,
    job,
    op,
    sensor,
)

from scanner.assets.automation import default_status
from scanner.cli import select_manifests
from scanner.runtime import catchup
from scanner.runtime.context import repo_state
from scanner.settings import get_settings

MAX_DAYS_PER_RUN = 60
JOB_NAME = "job_catch_up"
_ACTIVE = [
    DagsterRunStatus.QUEUED,
    DagsterRunStatus.NOT_STARTED,
    DagsterRunStatus.STARTING,
    DagsterRunStatus.STARTED,
]


def _plans() -> list[catchup.DatasetPlan]:
    state = repo_state()
    with psycopg.connect(get_settings().database_url) as conn:
        conn.read_only = True
        return catchup.plan_catch_up(conn, state, select_manifests(state.manifests, "all"))


@op
def catch_up_op(context: OpExecutionContext) -> None:
    state = repo_state()
    plans = _plans()
    context.log.info(catchup.format_plans(plans, catchup.unrecoverable(state)))
    results = catchup.run_catch_up(
        state, plans, MAX_DAYS_PER_RUN, lambda r: context.log.info(f"{r.dataset} {r.day} {r.status}")
    )
    failed = [r for r in results if r.status != "ok"]
    context.log.info(f"{len(results)} day(s) ingested, {len(failed)} not ok (they stay todo for the next round)")


@job(name=JOB_NAME, description="Ingest the local days that have no full-day run yet (scanner.runtime.catchup).")
def catch_up_job() -> None:
    catch_up_op()


@sensor(
    name="sensor_catch_up",
    job=catch_up_job,
    minimum_interval_seconds=3600,
    default_status=DefaultSensorStatus.RUNNING
    if default_status() == DefaultScheduleStatus.RUNNING
    else DefaultSensorStatus.STOPPED,
)
def sensor_catch_up(context: SensorEvaluationContext) -> RunRequest | SkipReason:
    if context.instance.get_runs(RunsFilter(job_name=JOB_NAME, statuses=_ACTIVE), limit=1):
        return SkipReason("a catch-up run is still active")
    todo = {p.label: len(p.todo) for p in _plans() if p.todo}
    if not todo:
        return SkipReason("every dataset is caught up")
    return RunRequest(tags={"todo_days": str(sum(todo.values()))})


CATCH_UP_JOBS = [catch_up_job]
CATCH_UP_SENSORS = [sensor_catch_up]
