"""Schedule defaults shared by both code locations (docs/architecture.md, "How data stays current").

Ingest, snapshot and capacity-map schedules live next to their assets in the ingestion location; the daily
compute job and its schedule are in scanner.assets.compute (analytics location).
"""

from __future__ import annotations

from dagster import DefaultScheduleStatus

from scanner.settings import get_settings


def default_status() -> DefaultScheduleStatus:
    """Default status of every schedule: RUNNING where SCANNER_SCHEDULES_DEFAULT_RUNNING is true (the orchestrator
    containers), STOPPED elsewhere. A toggle in the Dagster UI overrides either default and persists."""
    running = get_settings().schedules_default_running
    return DefaultScheduleStatus.RUNNING if running else DefaultScheduleStatus.STOPPED
