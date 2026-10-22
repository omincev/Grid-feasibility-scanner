"""Dagster code location `ops` (workspace.yaml): operational alerts only (infra/README.md, "Alerts").

- `sensor_run_failure_alert`: every failed run, in any code location, to ALERT_WEBHOOK_URL.
- `sensor_health_alert`: every 15 minutes, stale active sources, recent missing or incomplete days, and disk
  usage (scanner.runtime.alerts); each problem is sent when it appears and when it is resolved.

Both run by default only where the webhook is set and schedules run by default (the VPS containers); otherwise
the location loads with both stopped and nothing is sent. A separate location that imports neither the ingestion
nor the analytics modules, so the alerts keep running when either of those fails to load.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from dagster import (
    DefaultSensorStatus,
    Definitions,
    RunFailureSensorContext,
    SensorEvaluationContext,
    SkipReason,
    run_failure_sensor,
    sensor,
)

from scanner.runtime import alerts
from scanner.settings import get_settings


def default_status() -> DefaultSensorStatus:
    """RUNNING where the webhook is set and schedules run by default (SCANNER_SCHEDULES_DEFAULT_RUNNING), else
    STOPPED. A toggle in the Dagster UI overrides either default."""
    running = get_settings().schedules_default_running and alerts.AlertSettings().enabled
    return DefaultSensorStatus.RUNNING if running else DefaultSensorStatus.STOPPED


@run_failure_sensor(
    name="sensor_run_failure_alert",
    monitor_all_code_locations=True,
    minimum_interval_seconds=60,
    default_status=default_status(),
    description="Posts every failed run, in any code location, to ALERT_WEBHOOK_URL.",
)
def run_failure_alert(context: RunFailureSensorContext) -> None:
    settings = alerts.AlertSettings()
    run = context.dagster_run
    origin = run.remote_job_origin
    text = alerts.run_failure_text(
        job=run.job_name,
        run_id=run.run_id,
        location=origin.location_name if origin else None,
        partition=run.tags.get("dagster/partition"),
        message=context.failure_event.message,
        ui_url=settings.dagster_ui_url,
    )
    alerts.post(settings, f"Grid Opportunity Scanner: run failed ({run.job_name})", text)


@sensor(
    name="sensor_health_alert",
    minimum_interval_seconds=900,
    default_status=default_status(),
    description="Stale active sources, recent missing or incomplete days, and disk usage, to ALERT_WEBHOOK_URL "
    "when a problem appears or is resolved (scanner.runtime.alerts).",
)
def health_alert(context: SensorEvaluationContext) -> SkipReason:
    import psycopg

    settings = alerts.AlertSettings()
    if not settings.enabled:
        return SkipReason("ALERT_WEBHOOK_URL is not set")
    today = datetime.now(UTC).date()
    with psycopg.connect(get_settings().database_url) as conn:
        conn.read_only = True
        problems = alerts.source_problems(conn, today, settings.alert_gap_days)
    disk = alerts.disk_problem(settings.disk_alert_path, settings.disk_alert_percent)
    problems += [disk] if disk else []
    new, resolved = alerts.changes(json.loads(context.cursor) if context.cursor else [], problems)
    if new or resolved:
        text = alerts.changes_text(new, resolved, len(problems))
        alerts.post(settings, "Grid Opportunity Scanner: health", text)
    context.update_cursor(json.dumps(sorted(p.key for p in problems)))
    return SkipReason(f"{len(problems)} open problem(s): {len(new)} new, {len(resolved)} resolved")


defs = Definitions(sensors=[run_failure_alert, health_alert])
