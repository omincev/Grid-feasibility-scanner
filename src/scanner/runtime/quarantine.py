"""Review of quarantined ingest runs (ADR 0013; docs/quality.md layers 2 and 3).

Rows written under a soft fingerprint deviation carry quality 'quarantined'. Readers use them, labelled
unverified, until a person records a decision on their ingest run in ops.quarantine_decision (append-only;
the run's latest decision is in force):
- accept: the rows become 'verified', which clears the label;
- reject: every reader excludes the rows (a genuine data error, such as a DKK value in a EUR field).

core.observation_effective (migration 20261018080000) is the one place that derives `review_status` and
`usable` from stored quality and the decision in force; core.observation is never updated (ADR 0003).
As-of reads use core.observation_effective_asof(t), which applies only decisions with decided_at <= t, so
a backtest as of t sees the review state a person had recorded by then.

Corroboration evidence (docs/quality.md layer 3) for each quarantined value that is still the latest
version of its key: the values of independent series at the same interval, meaning the same entity,
equivalence class and unit, another source dataset, no issue time, latest version, not rejected. Nothing
is resampled or converted. A value is
- matched: at least one ok or verified reference, and every reference within the class tolerance;
- matched_unverified: only unverified references, all within the tolerance;
- mismatched: a reference differs by more than the tolerance;
- uncovered: no reference value at that interval, or the value itself is null.
Classes without a tolerance in TOLERANCE are not reconciled, so they have no independent source.

Nothing here runs on a schedule or from an asset. `accept_corroborated` is a tool for a person at the CLI
(`python -m scanner.cli quarantine ...`) and writes only when called with yes=True.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import psycopg

# docs/quality.md layer 3: equivalence class -> (tolerance, unit). Only series in that unit are compared.
TOLERANCE: dict[str, tuple[float, str]] = {"day_ahead_price": (0.01, "EUR/MWh")}
# Slack for binary floating point, so a difference of exactly the tolerance counts as within it.
FLOAT_SLACK = 1e-9

Bucket = Literal["matched", "matched_unverified", "mismatched", "uncovered"]
BUCKETS: tuple[Bucket, ...] = ("matched", "matched_unverified", "mismatched", "uncovered")
DECISIONS = ("accept", "reject")


@dataclass(frozen=True)
class Reference:
    """One independent value at the same interval as a quarantined value."""

    series_key: str
    value: float | None
    review_status: str  # ok, verified or unverified; rejected values are never references


def classify(value: float | None, refs: Sequence[Reference], tolerance: float) -> tuple[Bucket, float | None]:
    """Bucket of one quarantined value (module docstring) and its largest absolute difference."""
    numbers = [r for r in refs if r.value is not None]
    if value is None or not numbers:
        return "uncovered", None
    diff = max(abs(value - r.value) for r in numbers if r.value is not None)
    if diff > tolerance + FLOAT_SLACK:
        return "mismatched", diff
    if any(r.review_status in ("ok", "verified") for r in numbers):
        return "matched", diff
    return "matched_unverified", diff


@dataclass
class ClassEvidence:
    """Corroboration of one run's quarantined values of one equivalence class."""

    equivalence_class: str
    unit: str
    tolerance: float | None
    counts: Counter[str] = field(default_factory=Counter)
    max_abs_diff: float | None = None
    references: set[str] = field(default_factory=set)

    def add(self, value: float | None, refs: Sequence[Reference]) -> None:
        if self.tolerance is None:
            self.counts["uncovered"] += 1
            return
        bucket, diff = classify(value, refs, self.tolerance)
        self.counts[bucket] += 1
        if diff is not None:
            self.max_abs_diff = diff if self.max_abs_diff is None else max(self.max_abs_diff, diff)
            self.references.update(r.series_key for r in refs if r.value is not None)

    @property
    def values(self) -> int:
        return sum(self.counts.values())

    @property
    def status(self) -> str:
        if self.values and self.counts["matched"] == self.values:
            return "corroborated"
        if self.counts["uncovered"] == self.values:
            return "no independent source"
        return "not corroborated"


@dataclass
class QuarantinedRun:
    run_id: str
    source: str
    dataset: str
    partition_key: str | None
    started_at: datetime
    deviations: list[str]
    rows: int  # quarantined rows that are still the latest version of their key
    superseded: int  # quarantined rows a later revision replaced
    decision: str | None = None  # the decision in force, if any
    decided_by: str | None = None
    decided_at: datetime | None = None
    evidence: list[ClassEvidence] = field(default_factory=list)
    # [first interval_start, last interval_end) of the quarantined rows. A soft run quarantines only rows in
    # its partition day; older runs (before that rule) may show a wider span.
    span: tuple[datetime, datetime] | None = None

    @property
    def label(self) -> str:
        return f"{self.source}.{self.dataset}"

    @property
    def outcome(self) -> str:
        """corroborated (every current value matched), no independent source, not corroborated, or no
        current rows."""
        statuses = {e.status for e in self.evidence}
        if not statuses:
            return "no current rows"
        if statuses == {"corroborated"}:
            return "corroborated"
        if statuses == {"no independent source"}:
            return "no independent source"
        return "not corroborated"


def plan_accepts(runs: Iterable[QuarantinedRun]) -> list[QuarantinedRun]:
    """Runs `accept_corroborated` would accept: no decision yet (a person's accept or reject is never
    overridden) and every current quarantined value matched by an ok or verified independent value."""
    return [r for r in runs if r.decision is None and r.outcome == "corroborated"]


def validate_decision(decision: str, decided_by: str, reason: str) -> None:
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}, not {decision!r}")
    if not decided_by.strip() or not reason.strip():
        raise ValueError("a decision needs the name of the person deciding (--by) and a reason (--reason)")


_RUNS_SQL = """
with q as (
    select o.run_id,
           count(*) filter (where n.newer is null) as current_rows,
           count(n.newer) as superseded_rows,
           min(o.interval_start) as first_start,
           max(o.interval_end) as last_end
    from core.observation o
    left join lateral (
        select true as newer
        from core.observation x
        where x.series_id = o.series_id and x.interval_start = o.interval_start
          and x.issued_at is not distinct from o.issued_at and x.ingested_at > o.ingested_at
        limit 1
    ) n on true
    where o.quality = 'quarantined'
    group by o.run_id
)
select r.run_id::text, r.source, r.dataset, r.partition_key, r.started_at,
       coalesce(f.deviations, '[]'::jsonb), q.current_rows, q.superseded_rows,
       d.decision, d.decided_by, d.decided_at, q.first_start, q.last_end
from q
join ops.ingest_run r on r.run_id = q.run_id
left join ops.fingerprint f on f.run_id = q.run_id
left join ops.quarantine_decision_asof('infinity') d on d.run_id = q.run_id
where %(dataset)s::text is null or r.dataset = %(dataset)s::text
order by r.source, r.dataset, r.partition_key, r.started_at
"""

_ROWS_SQL = """
select o.run_id::text, s.entity_id, s.unit, ce.equivalence_class, ce.source || '.' || ce.dataset,
       o.interval_start, o.interval_end, o.issued_at, o.value
from core.observation o
join core.series s on s.series_id = o.series_id
join ref.codebook_entry ce on ce.entry_id = s.entry_id
where o.quality = 'quarantined' and o.run_id = any(%s)
  and not exists (
      select 1 from core.observation x
      where x.series_id = o.series_id and x.interval_start = o.interval_start
        and x.issued_at is not distinct from o.issued_at and x.ingested_at > o.ingested_at
  )
"""

_REF_SERIES_SQL = """
select s.series_id, s.series_key, s.entity_id, s.unit, ce.equivalence_class, ce.source || '.' || ce.dataset
from core.series s
join ref.codebook_entry ce on ce.entry_id = s.entry_id
where ce.equivalence_class = any(%s)
"""

_REF_VALUES_SQL = """
select series_id, interval_start, interval_end, value, review_status
from core.observation_effective
where series_id = any(%s) and interval_start >= %s and interval_start <= %s and issued_at is null and usable
"""


def quarantined_runs(conn: psycopg.Connection[Any], dataset: str | None = None) -> list[QuarantinedRun]:
    """Every ingest run with quarantined rows (optionally of one dataset), with its soft deviations, the
    decision in force and its corroboration evidence."""
    runs = [
        QuarantinedRun(
            run_id=run_id,
            source=source,
            dataset=ds,
            partition_key=partition,
            started_at=started,
            deviations=[f"{d['severity']}:{d['level']}:{d['detail']}" for d in deviations],
            rows=current,
            superseded=superseded,
            decision=decision,
            decided_by=by,
            decided_at=at,
            span=(first, last),
        )
        for (
            run_id, source, ds, partition, started, deviations, current, superseded, decision, by, at, first, last
        ) in conn.execute(_RUNS_SQL, {"dataset": dataset})
    ]  # fmt: skip
    attach_evidence(conn, runs)
    return runs


def attach_evidence(conn: psycopg.Connection[Any], runs: Sequence[QuarantinedRun]) -> None:
    """Fill each run's evidence, one ClassEvidence per equivalence class of its current quarantined rows."""
    by_run: dict[str, list[tuple[Any, ...]]] = {}
    for row in conn.execute(_ROWS_SQL, ([uuid.UUID(r.run_id) for r in runs],)):
        by_run.setdefault(row[0], []).append(row)
    ref_series = conn.execute(_REF_SERIES_SQL, (list(TOLERANCE),)).fetchall()
    for run in runs:
        rows = by_run.get(run.run_id, [])
        refs = _references(conn, rows, ref_series)
        evidence: dict[tuple[str, str], ClassEvidence] = {}
        for _, entity, unit, cls, dataset_key, start, end, issued, value in rows:
            tol = TOLERANCE.get(cls)
            tolerance = tol[0] if tol is not None and tol[1] == unit else None
            ev = evidence.setdefault((cls, unit), ClassEvidence(cls, unit, tolerance))
            same = refs.get((entity, cls, unit, start, end), []) if issued is None else []
            ev.add(value, [ref for dk, ref in same if dk != dataset_key])
        run.evidence = [evidence[k] for k in sorted(evidence)]


RefKey = tuple[str, str, str, datetime, datetime]


def _references(
    conn: psycopg.Connection[Any], rows: Sequence[tuple[Any, ...]], ref_series: Sequence[tuple[Any, ...]]
) -> dict[RefKey, list[tuple[str, Reference]]]:
    """Usable values of candidate reference series over the span of one run's comparable rows, keyed by
    entity, class, unit and interval, each with its source dataset."""
    comparable = [r for r in rows if r[3] in TOLERANCE and r[7] is None]
    wanted = {(r[1], r[3], r[2]) for r in comparable}
    series = {sid: (key, e, c, u, dk) for sid, key, e, u, c, dk in ref_series if (e, c, u) in wanted}
    if not series:
        return {}
    starts = [r[5] for r in comparable]
    out: dict[RefKey, list[tuple[str, Reference]]] = {}
    for sid, start, end, value, status in conn.execute(_REF_VALUES_SQL, (list(series), min(starts), max(starts))):
        key, entity, cls, unit, dataset_key = series[sid]
        out.setdefault((entity, cls, unit, start, end), []).append((dataset_key, Reference(key, value, status)))
    return out


def has_quarantined_rows(conn: psycopg.Connection[Any], run_id: uuid.UUID) -> bool:
    row = conn.execute(
        "select exists (select 1 from core.observation where run_id = %s and quality = 'quarantined')", (run_id,)
    ).fetchone()
    return bool(row and row[0])


def record_decision(
    conn: psycopg.Connection[Any], run_id: uuid.UUID, decision: str, decided_by: str, reason: str
) -> int:
    """Append one decision for an ingest run with quarantined rows and commit; returns its decision_id."""
    validate_decision(decision, decided_by, reason)
    if not has_quarantined_rows(conn, run_id):
        raise ValueError(f"ingest run {run_id} has no quarantined rows")
    row = conn.execute(
        "insert into ops.quarantine_decision (run_id, decided_by, decision, reason) values (%s, %s, %s, %s) "
        "returning decision_id",
        (run_id, decided_by.strip(), decision, reason.strip()),
    ).fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


def accept_corroborated(
    conn: psycopg.Connection[Any], *, decided_by: str, reason: str, dataset: str | None = None, yes: bool = False
) -> tuple[list[QuarantinedRun], list[int]]:
    """The runs plan_accepts selects and, only if `yes`, one 'accept' decision each (decision IDs).

    A run that received a decision since it was listed is left alone. Never call this from a schedule or
    an asset: accepting is a person's decision (ADR 0014).
    """
    validate_decision("accept", decided_by, reason)
    plan = plan_accepts(quarantined_runs(conn, dataset))
    if not yes:
        conn.rollback()
        return plan, []
    ids = []
    for run in plan:
        row = conn.execute(
            "insert into ops.quarantine_decision (run_id, decided_by, decision, reason) "
            "select %(run)s, %(by)s, 'accept', %(reason)s "
            "where not exists (select 1 from ops.quarantine_decision where run_id = %(run)s) "
            "returning decision_id",
            {"run": uuid.UUID(run.run_id), "by": decided_by.strip(), "reason": reason.strip()},
        ).fetchone()
        if row is not None:
            ids.append(int(row[0]))
    conn.commit()
    return plan, ids


def _utc(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ")


def _number(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.6g}"


def format_evidence(e: ClassEvidence) -> str:
    if e.status == "no independent source":
        why = "" if e.tolerance is not None else " (class not reconciled, docs/quality.md layer 3)"
        return f"{e.equivalence_class}: no independent source{why}; {e.values} values"
    counts = ", ".join(f"{b.replace('_', ' ')} {e.counts[b]}" for b in BUCKETS)
    return (
        f"{e.equivalence_class}: {e.status}; {e.values} values: {counts}; "
        f"max |diff| {_number(e.max_abs_diff)} {e.unit} (tolerance {e.tolerance}); "
        f"references: {', '.join(sorted(e.references)) or 'none'}"
    )


def format_runs(runs: Sequence[QuarantinedRun]) -> str:
    """Runs grouped by dataset and partition, with deviations and evidence per class."""
    lines: list[str] = []
    label = None
    for r in runs:
        if r.label != label:
            label = r.label
            lines.append(label)
        decided = "none"
        if r.decided_at is not None:
            decided = f"{r.decision} by {r.decided_by} at {_utc(r.decided_at)}"
        superseded = f" ({r.superseded} superseded)" if r.superseded else ""
        span = "" if r.span is None else f" over [{_utc(r.span[0])}, {_utc(r.span[1])})"
        lines.append(
            f"  {r.partition_key}  run {r.run_id}  quarantined rows {r.rows}{superseded}{span}  "
            f"outcome: {r.outcome}  decision: {decided}"
        )
        lines += [f"    deviation {d}" for d in r.deviations]
        lines += [f"    {format_evidence(e)}" for e in r.evidence]
    return "\n".join(lines)


def format_summary(runs: Sequence[QuarantinedRun]) -> str:
    """Per dataset: quarantined runs, undecided, and how many are corroborated or have no independent source."""
    head = (
        f"{'dataset':<42} {'runs':>5} {'undecided':>9} {'corroborated':>12} {'no_indep_source':>15} "
        f"{'not_corroborated':>16} {'rows':>7}"
    )
    lines = [head]
    for label in dict.fromkeys(r.label for r in runs):
        rs = [r for r in runs if r.label == label]
        n = Counter(r.outcome for r in rs)
        undecided = sum(r.decision is None for r in rs)
        rows = sum(r.rows for r in rs)
        lines.append(
            f"{label:<42} {len(rs):>5} {undecided:>9} {n['corroborated']:>12} {n['no independent source']:>15} "
            f"{n['not corroborated']:>16} {rows:>7}"
        )
    return "\n".join(lines)
