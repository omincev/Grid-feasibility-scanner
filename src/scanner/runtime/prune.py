"""Retention of feature runs (ADR 0003: observations are never overwritten; derived runs can be recomputed).

The daily compute job appends a full feature run (~25k rows) each day. `plan_prune` picks the runs that can go
and `apply_prune` deletes them: the rows in feat.feature_value and the run's ops.compute_run row.

What is kept, per feature (ops.compute_run.name):
- the newest `keep` runs that finished ok or partial, and any run still running;
- every feature run a protected derived run points at. Scores name their feature runs explicitly:
  feat.score.breakdown -> features -> <key> -> run_id (and unverified_inputs); an opportunity row names its
  score run (reasons.score_run_id); valuation rows are searched too (val.run.inputs). Protected derived runs are
  the ones served or exported (the latest ok or partial run per scenario, kind and name, as feat.scenario_run
  chooses them) and every run of a scenario with an approver (ref.scenario.approved_by). With protect="all"
  every score, opportunity and valuation row protects its runs, which keeps all runs of a daily pipeline.

Older score rows of an unapproved scenario that a later run superseded are not served; they keep the run IDs in
their breakdown, which may then no longer resolve.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import psycopg

Protect = Literal["served", "all"]

_UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


@dataclass(frozen=True)
class PrunableRun:
    run_id: uuid.UUID
    name: str
    status: str
    started_at: datetime
    rows: int


@dataclass(frozen=True)
class PrunePlan:
    delete: list[PrunableRun]
    kept: int  # feature runs that stay
    protected: int  # of those, kept only because a derived run references them

    @property
    def rows(self) -> int:
        return sum(r.rows for r in self.delete)


def _uuids(conn: psycopg.Connection[Any], column: str, table: str, run_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """Every UUID written anywhere in `column` (jsonb) of the table's rows of the given runs."""
    rows = conn.execute(
        f"select distinct m[1] from {table}, regexp_matches({column}::text, %s, 'g') m where run_id = any(%s)",
        (_UUID, run_ids),
    ).fetchall()
    return {uuid.UUID(r[0]) for r in rows}


def protected_derived_runs(conn: psycopg.Connection[Any], protect: Protect) -> list[uuid.UUID]:
    """Score, opportunity and valuation runs whose references must resolve."""
    if protect == "all":
        rows = conn.execute("select run_id from ops.compute_run where kind in ('score', 'valuation')").fetchall()
    else:
        rows = conn.execute(
            """
            select run_id from feat.scenario_run where kind in ('score', 'valuation')
            union
            select r.run_id from ops.compute_run r
            join ref.scenario s on s.scenario_id = r.scenario_id
            where r.kind in ('score', 'valuation') and s.approved_by is not null
            union
            select r.run_id from (
                select distinct on (kind, name) run_id, kind from ops.compute_run
                where kind in ('score', 'valuation') and scenario_id is null and status in ('ok', 'partial')
                order by kind, name, started_at desc
            ) r
            """
        ).fetchall()
    return [r[0] for r in rows]


def referenced_feature_runs(conn: psycopg.Connection[Any], protect: Protect = "served") -> set[uuid.UUID]:
    """Run IDs named by the protected score, opportunity and valuation rows (module docstring)."""
    derived = protected_derived_runs(conn, protect)
    score_runs = list({*derived, *_uuids(conn, "reasons", "feat.opportunity", derived)})
    return (
        _uuids(conn, "breakdown", "feat.score", score_runs)
        | _uuids(conn, "inputs", "val.run", derived)
        | _uuids(conn, "reasons", "feat.opportunity", derived)
    )


def plan_prune(conn: psycopg.Connection[Any], keep: int, protect: Protect = "served") -> PrunePlan:
    if keep < 1:
        raise ValueError("--keep must be at least 1: the newest feature run of each feature is what scores read")
    referenced = referenced_feature_runs(conn, protect)
    rows = conn.execute(
        """
        select r.run_id, r.name, r.status, r.started_at, coalesce(c.n, 0)
        from ops.compute_run r
        left join (select run_id, count(*) n from feat.feature_value group by run_id) c on c.run_id = r.run_id
        where r.kind = 'feature'
        order by r.name, r.started_at desc
        """
    ).fetchall()
    delete: list[PrunableRun] = []
    kept = protected = 0
    seen_good: dict[str, int] = {}
    for run_id, name, status, started_at, n in rows:
        run = PrunableRun(run_id, name, status, started_at, n)
        if status == "running":
            keep_it, by_ref = True, False
        elif status in ("ok", "partial"):
            seen_good[name] = seen_good.get(name, 0) + 1
            keep_it, by_ref = seen_good[name] <= keep, False
        else:  # failed or skipped: only worth keeping once newer than the oldest kept good run
            keep_it, by_ref = seen_good.get(name, 0) < keep, False
        if not keep_it and run_id in referenced:
            keep_it, by_ref = True, True
        if keep_it:
            kept += 1
            protected += by_ref
        else:
            delete.append(run)
    return PrunePlan(delete, kept, protected)


def apply_prune(conn: psycopg.Connection[Any], plan: PrunePlan) -> int:
    """Delete the planned runs in one transaction; returns feature_value rows deleted."""
    ids = [r.run_id for r in plan.delete]
    if not ids:
        return 0
    deleted = conn.execute("delete from feat.feature_value where run_id = any(%s)", (ids,)).rowcount
    conn.execute("delete from ops.compute_run where run_id = any(%s) and kind = 'feature'", (ids,))
    conn.commit()
    return deleted


def format_plan(plan: PrunePlan, applied: int | None = None) -> str:
    lines = [
        f"{r.name:<28} {r.started_at:%Y-%m-%d %H:%M} {r.status:<8} {r.run_id} {r.rows:>8} rows" for r in plan.delete
    ]
    summary = (
        f"{len(plan.delete)} feature run(s), {plan.rows} feature_value rows "
        f"{'deleted' if applied is not None else 'would be deleted'}; "
        f"{plan.kept} kept ({plan.protected} only because a served or approved score references them)"
    )
    return "\n".join([*lines, summary])


def prune_feature_runs(database_url: str, keep: int, *, protect: Protect = "served", yes: bool = False) -> str:
    """Plan (and with `yes` apply) the pruning; returns the report text. The caller's role must be allowed to
    delete from feat.feature_value and ops.compute_run (the owner role, not scanner_analytics)."""
    with psycopg.connect(database_url) as conn:
        conn.read_only = not yes
        plan = plan_prune(conn, keep, protect)
        applied = apply_prune(conn, plan) if yes else None
    return format_plan(plan, applied)
