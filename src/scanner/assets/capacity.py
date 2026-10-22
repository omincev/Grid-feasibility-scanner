"""Capacity-map asset (WP1.5): a thin wrapper around scanner.runtime.capacity_map.load_capacity_map.

Not partitioned: the source shows only its current state, with a publication date per station. Weekly
on Monday after the 06:00 snapshot captures; the DSO side changes quarterly and the TSO side monthly,
and each run writes only facts that changed. Its description and metadata show the codebook entries it
writes and the Decisions sections of the two dossiers of the map (scanner.assets.rules).
"""

import psycopg
from dagster import AssetExecutionContext, MaterializeResult, ScheduleDefinition, asset, define_asset_job

from scanner.assets.automation import default_status
from scanner.assets.keys import CAPACITY_MAP_KEY
from scanner.assets.rules import source_rules
from scanner.runtime.capacity_map import load_capacity_map
from scanner.runtime.context import repo_state
from scanner.settings import get_settings

DOSSIERS = ("docs/sources/dk/dso-capacity-maps.md", "docs/sources/dk/energinet-transmission-capacity.md")
_RULES, _METADATA = source_rules(repo_state(), "kapacitetskort", DOSSIERS)


@asset(
    key=CAPACITY_MAP_KEY,
    group_name="capacity",
    kinds={"python", "postgres"},
    description="Danish shared capacity map (Kapacitetskortet_V2) -> substations, DSO areas and core.fact rows "
    "(one fact per published field, valid from the station's publication date; a published null stays null).\n\n"
    + _RULES,
    metadata=_METADATA,
)
def kapacitetskort(context: AssetExecutionContext) -> MaterializeResult:
    with psycopg.connect(get_settings().database_url, autocommit=True) as conn:
        report = load_capacity_map(conn)
    context.log.info(report.text())
    return MaterializeResult(
        metadata={
            "run_id": report.run_id,
            "facts_written": report.facts_written,
            "stations": sum(report.entities_per_layer.values()),
            "no_zone": len(report.no_zone),
            "no_municipality": len(report.no_municipality),
            "dso_ambiguous": len(report.dso_ambiguous),
            "lower_above_upper": len(report.quirks),
            "report": report.text(),
        }
    )


capacity_job = define_asset_job("job_capacity_kapacitetskort", selection=[CAPACITY_MAP_KEY])
CAPACITY_ASSETS = [kapacitetskort]
CAPACITY_SCHEDULES = [
    ScheduleDefinition(
        name="schedule_capacity_kapacitetskort",
        job=capacity_job,
        cron_schedule="30 6 * * 1",
        execution_timezone="Europe/Copenhagen",
        default_status=default_status(),
    )
]
