"""Catch-up after downtime or on a fresh server (docs/architecture.md, "How data stays current").

For each ingestible manifest, `plan_catch_up` lists the local days from the dataset's history start (or `since`)
to yesterday that still need a run, from ops.ingest_run. `scanner.cli catch-up` and the ingestion location's
catch-up job then ingest those days through the same code path as `scanner.cli ingest`
(`scanner.cli.ingest_dataset`: run_ingest per day, sequential within a dataset, under the per-dataset advisory
lock `scanner.core.db.lock_dataset`, so it can run next to the schedules).

A day is done once an ok run of its partition started after the day had ended: that run saw the whole day, and
its fingerprint and row count are the day's completeness check (serving.source_completeness). What is left:
- todo: no such run (never ingested, only runs on the day itself, or failed runs). Ingested again.
- blocked: the newest run of the day was blocked by a hard deviation. Not retried: a person decides (bead).
- incomplete: a full-day run received fewer rows than expected. Not retried: the source lacks them, and another
  run would only repeat the question; reported as a gap.
Today's partition is left to the schedules. Sources that only show their current state (the snapshots in
registry/snapshots.yaml and the capacity map) have no history to fetch; `unrecoverable` lists them as gaps.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg

from scanner.core.manifest import Manifest
from scanner.core.registry import load_snapshots
from scanner.core.time import local_day, local_day_bounds
from scanner.runtime.context import RepoState


@dataclass(frozen=True)
class DatasetPlan:
    manifest: Manifest
    todo: list[date] = field(default_factory=list)
    blocked: list[date] = field(default_factory=list)
    incomplete: list[date] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.manifest.source}.{self.manifest.dataset}"


def last_day(manifest: Manifest, tz: str, now: datetime) -> date:
    """The last local day to catch up: yesterday, or the end of the source's history."""
    yesterday = local_day(now, tz) - timedelta(days=1)
    return min(yesterday, manifest.history_end_local) if manifest.history_end_local else yesterday


def plan_dataset(
    conn: psycopg.Connection[Any], manifest: Manifest, tz: str, since: date | None, now: datetime
) -> DatasetPlan:
    first = max(since, manifest.history_start_local) if since else manifest.history_start_local
    last = last_day(manifest, tz, now)
    if first > last:
        return DatasetPlan(manifest)
    # Per partition: when the newest ok run started, and the newest run overall with its status and counts.
    rows = conn.execute(
        """
        select partition_key,
               max(started_at) filter (where status = 'ok'),
               (array_agg(status order by started_at desc))[1],
               (array_agg(rows_received order by started_at desc) filter (where status = 'ok'))[1],
               (array_agg(rows_expected order by started_at desc) filter (where status = 'ok'))[1]
        from ops.ingest_run
        where source = %s and dataset = %s and partition_key between %s and %s
        group by partition_key
        """,
        (manifest.source, manifest.dataset, first.isoformat(), last.isoformat()),
    ).fetchall()
    seen = {r[0]: r[1:] for r in rows}
    plan = DatasetPlan(manifest)
    for i in range((last - first).days + 1):
        day = first + timedelta(days=i)
        last_ok, newest, received, expected = seen.get(day.isoformat(), (None, None, None, None))
        _, day_end = local_day_bounds(day, tz)
        if last_ok is not None and last_ok >= day_end:
            if expected is not None and received is not None and received < expected:
                plan.incomplete.append(day)
        elif newest == "blocked":
            plan.blocked.append(day)
        else:
            plan.todo.append(day)
    return plan


def plan_catch_up(
    conn: psycopg.Connection[Any],
    state: RepoState,
    manifests: list[Manifest],
    since: date | None = None,
    now: datetime | None = None,
) -> list[DatasetPlan]:
    now = now or datetime.now(UTC)
    return [plan_dataset(conn, m, state.countries[m.country].timezone, since, now) for m in manifests]


def unrecoverable(state: RepoState) -> list[str]:
    """Sources that cannot be caught up: they publish only their current state, so a missed capture is a gap."""
    snapshots = [s.id for s in load_snapshots(state.root) if s.status == "active"]
    return [*snapshots, "capacity/kapacitetskort"]


def run_catch_up(
    state: RepoState,
    plans: list[DatasetPlan],
    max_days: int | None = None,
    on_day: Callable[[Any], None] = lambda r: None,
) -> list[Any]:
    """Ingest each dataset's todo days (at most `max_days` per dataset, oldest first), datasets one after the
    other: the same run_ingest path and per-dataset sequencing as `scanner.cli ingest`. Failed days are recorded
    and the loop goes on; they stay todo for the next catch-up."""
    from scanner.cli import ingest_dataset

    results: list[Any] = []
    for p in plans:
        days = p.todo if max_days is None else p.todo[:max_days]
        if days:
            results += ingest_dataset(state, p.manifest, days[0], None, on_day, days=days)
    return results


def format_plans(plans: list[DatasetPlan], gaps: list[str]) -> str:
    lines = [f"{'dataset':<40} {'todo':>5} {'blocked':>7} {'incomplete':>10}"]
    for p in plans:
        lines.append(f"{p.label:<40} {len(p.todo):>5} {len(p.blocked):>7} {len(p.incomplete):>10}")
    for p in plans:
        if p.blocked:
            lines.append(f"gap {p.label}: blocked by a hard deviation, needs a person: {_days(p.blocked)}")
        if p.incomplete:
            lines.append(f"gap {p.label}: full-day run received fewer rows than expected: {_days(p.incomplete)}")
    if gaps:
        lines.append(f"gap (current state only, cannot be caught up): {', '.join(gaps)}")
    return "\n".join(lines)


def _days(days: list[date]) -> str:
    shown = ", ".join(d.isoformat() for d in days[:10])
    return shown + (f" ... ({len(days)} days)" if len(days) > 10 else "")
