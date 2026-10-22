"""Run IDs and code version for derived data: features, scores and valuation.

Every run_id written to feat.feature_value, feat.score, feat.opportunity or val.run resolves to a row in
ops.compute_run (migration 20261018020000, foreign keys enforce it). Usage:

    with compute_run(conn, kind="score", name="congestion", params={...}, scenario_id="demo_default") as run:
        ...write rows with run.run_id and run.code_version...
        run.status = "partial"; run.notes.append("why something was not computed")
        run.params["unverified_values"] = 3  # what the run learnt about its inputs, stored when it closes

The run row is committed before any work starts, so a crash still leaves a 'failed' row behind. Notes
are stored in ops.compute_run.error only when the status is 'partial' or 'skipped'. `run.params` is
written when the run opens and again when it closes successfully. Score and valuation runs name the scenario
they computed (ops.compute_run.scenario_id, scanner.runtime.scenarios); feature runs are scenario-independent.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

import psycopg
from psycopg.types.json import Jsonb

from scanner import REPO_ROOT

RunKind = Literal["feature", "score", "valuation"]
RunStatus = Literal["running", "ok", "partial", "skipped", "failed"]


def code_version() -> str:
    """`GIT_SHA` from the environment, else `git rev-parse --short HEAD`, else 'unknown'."""
    env = os.environ.get("GIT_SHA", "").strip()
    if env:
        return env
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() or "unknown"


@dataclass
class ComputeRun:
    run_id: uuid.UUID
    kind: RunKind
    name: str
    code_version: str
    status: RunStatus = "ok"
    notes: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    scenario_id: str | None = None


def start_compute_run(
    conn: psycopg.Connection[Any],
    *,
    kind: RunKind,
    name: str,
    params: Mapping[str, Any],
    scenario_id: str | None = None,
) -> ComputeRun:
    """Insert a 'running' row and commit it. `params` must be JSON-serialisable."""
    run = ComputeRun(
        run_id=uuid.uuid4(),
        kind=kind,
        name=name,
        code_version=code_version(),
        params=dict(params),
        scenario_id=scenario_id,
    )
    conn.execute(
        "insert into ops.compute_run (run_id, kind, name, status, code_version, params, scenario_id) "
        "values (%s, %s, %s, 'running', %s, %s, %s)",
        (run.run_id, kind, name, run.code_version, Jsonb(run.params), scenario_id),
    )
    conn.commit()
    return run


def finish_compute_run(
    conn: psycopg.Connection[Any],
    run_id: uuid.UUID,
    *,
    status: RunStatus,
    error: str | None = None,
    params: Mapping[str, Any] | None = None,
) -> None:
    """Close a run; `params`, if given, replace the ones stored when it opened. The update runs in the
    transaction of the run's work, so finished_at is clock_timestamp(), not now() (that transaction's start)."""
    conn.execute(
        "update ops.compute_run set finished_at = clock_timestamp(), status = %s, error = %s, "
        "params = coalesce(%s, params) "
        "where run_id = %s",
        (status, error[:4000] if error else None, None if params is None else Jsonb(dict(params)), run_id),
    )
    conn.commit()


@contextmanager
def compute_run(
    conn: psycopg.Connection[Any],
    *,
    kind: RunKind,
    name: str,
    params: Mapping[str, Any],
    scenario_id: str | None = None,
) -> Iterator[ComputeRun]:
    """Open a run, yield it, and close it with its final status. Exceptions roll back and mark it failed."""
    run = start_compute_run(conn, kind=kind, name=name, params=params, scenario_id=scenario_id)
    try:
        yield run
    except BaseException as exc:
        conn.rollback()
        run.status = "failed"
        finish_compute_run(conn, run.run_id, status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    reason = "; ".join(run.notes) if run.status in ("partial", "skipped") else None
    finish_compute_run(conn, run.run_id, status=run.status, error=reason or None, params=run.params)
