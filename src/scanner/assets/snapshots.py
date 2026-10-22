"""One asset and schedule per active snapshot in registry/snapshots.yaml (WP1.4).

Snapshots whose captures a parser turns into facts (on_change `parse:<name>`) show the Decisions section of
that parser's dossier (PARSER_DOSSIERS, scanner.assets.rules).
"""

from typing import Any

from dagster import (
    AssetExecutionContext,
    AssetsDefinition,
    MaterializeResult,
    MetadataValue,
    ScheduleDefinition,
    asset,
    define_asset_job,
)

from scanner import REPO_ROOT
from scanner.assets.automation import default_status
from scanner.assets.keys import snapshot_key
from scanner.assets.rules import DECISIONS, section
from scanner.core.registry import Snapshot, load_snapshots
from scanner.runtime.snapshots import run_snapshot

# Parser (scanner.runtime.snapshot_parsers.PARSERS) -> the dossier that records its normalisation decisions.
PARSER_DOSSIERS = {"connection_queue": "docs/sources/dk/energinet-connection-queue.md"}


def _build(s: Snapshot) -> AssetsDefinition:
    description = f"Capture of {s.url} ({s.kind}, {s.render}). Owner: {s.owner}."
    metadata: dict[str, Any] = {"url": s.url or "", "on_change": s.on_change}
    if dossier := PARSER_DOSSIERS.get(s.on_change.removeprefix("parse:")):
        decisions = section((REPO_ROOT / dossier).read_text(encoding="utf-8"), DECISIONS) or "none recorded"
        description += f"\n\n**Decisions** (dossier `{dossier}`)\n\n{decisions}"
        metadata |= {"dossier": MetadataValue.path(dossier), "decisions": MetadataValue.md(decisions)}

    @asset(
        key=snapshot_key(s),
        group_name="snapshots",
        kinds={"python"},
        description=description,
        metadata=metadata,
    )
    def _snap(context: AssetExecutionContext) -> MaterializeResult:
        out = run_snapshot(s)
        if out.error:
            context.log.warning(f"{s.id}: {out.error}")
        return MaterializeResult(
            metadata={
                "status_code": out.status_code or -1,
                "sha256": out.sha256 or "",
                "changed": out.changed,
                "raw_uri": out.raw_uri or "",
                "error": out.error or "",
                "facts_written": out.facts_written,
                "notes": "\n".join(out.notes) or "none",
            }
        )

    return _snap


def build_snapshot_defs() -> tuple[list[AssetsDefinition], list[ScheduleDefinition]]:
    assets: list[AssetsDefinition] = []
    schedules: list[ScheduleDefinition] = []
    for s in load_snapshots(REPO_ROOT):
        if s.status != "active":
            continue
        a = _build(s)
        assets.append(a)
        job = define_asset_job(f"job_snapshot_{snapshot_key(s).path[-1]}", selection=[snapshot_key(s)])
        schedules.append(
            ScheduleDefinition(
                name=f"schedule_snapshot_{snapshot_key(s).path[-1]}",
                job=job,
                cron_schedule=s.schedule,
                execution_timezone=s.timezone,
                default_status=default_status(),
            )
        )
    return assets, schedules
