"""Database access for the runtime. Writes are append-only (ADR 0003)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from scanner.core.fingerprint import Deviation, Fingerprint, Severity
from scanner.core.sensitivity import Sensitivity


@dataclass(frozen=True)
class SeriesSpec:
    series_key: str
    entry_id: str
    entity_id: str
    unit: str
    resolution: timedelta
    kind: str
    sensitivity: Sensitivity
    licence: str | None


@dataclass(frozen=True)
class ObservationRow:
    series_key: str
    interval_start: datetime
    interval_end: datetime
    issued_at: datetime | None
    value: float | None
    quality: str = "ok"


def connect(url: str) -> psycopg.Connection[Any]:
    return psycopg.connect(url)


def ensure_series(conn: psycopg.Connection[Any], spec: SeriesSpec) -> int:
    """The series ID, creating the series if needed. A sensitivity raised in the codebook since the series
    was created is applied (ADR 0008); a lowered one is not (a person decides that)."""
    row = conn.execute(
        """
        insert into core.series (series_key, entry_id, entity_id, unit, resolution, kind, sensitivity, licence)
        values (%s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (series_key) do update set sensitivity = greatest(core.series.sensitivity, excluded.sensitivity)
        returning series_id
        """,
        (
            spec.series_key,
            spec.entry_id,
            spec.entity_id,
            spec.unit,
            spec.resolution,
            spec.kind,
            spec.sensitivity,
            spec.licence,
        ),
    ).fetchone()
    assert row is not None
    return int(row[0])


class DuplicateKeyError(ValueError):
    pass


def write_observations(
    conn: psycopg.Connection[Any],
    rows: Sequence[ObservationRow],
    series_ids: dict[str, int],
    run_id: uuid.UUID,
) -> int:
    """Insert rows that are new or differ from the latest stored version of their key. Returns rows written.

    A row differs if its value or interval_end changed (written with the quality it carries), or if it
    is quarantined while the latest version is ok: the same value then gets a quarantined version under
    this run, so a row first written ok (e.g. through the previous day's lookahead) carries the label
    of the run that fingerprinted its day. The reverse never writes: an unchanged value does not lose
    its quarantined label to a clean run; only a person's decision clears it (docs/quality.md layer 2).
    """
    seen: set[tuple[str, datetime, datetime | None]] = set()
    for r in rows:
        key = (r.series_key, r.interval_start, r.issued_at)
        if key in seen:
            raise DuplicateKeyError(f"duplicate observation key in batch: {key}")
        seen.add(key)
    with conn.transaction():
        # Dropped explicitly as well: inside an outer transaction this block is a savepoint, so
        # "on commit drop" would not fire between two calls.
        conn.execute("drop table if exists pg_temp.stage_obs")
        conn.execute(
            """
            create temp table stage_obs (
                series_id bigint, interval_start timestamptz, interval_end timestamptz,
                issued_at timestamptz, value double precision, quality core.quality
            ) on commit drop
            """
        )
        with conn.cursor().copy(
            "copy stage_obs (series_id, interval_start, interval_end, issued_at, value, quality) from stdin"
        ) as copy:
            for r in rows:
                copy.write_row(
                    (
                        series_ids[r.series_key],
                        r.interval_start,
                        r.interval_end,
                        r.issued_at,
                        r.value,
                        r.quality,
                    )
                )
        cur = conn.execute(
            """
            insert into core.observation (series_id, interval_start, interval_end, issued_at, value, run_id, quality)
            select s.series_id, s.interval_start, s.interval_end, s.issued_at, s.value, %s, s.quality
            from stage_obs s
            left join lateral (
                select o.value, o.interval_end, o.quality
                from core.observation o
                where o.series_id = s.series_id
                  and o.interval_start = s.interval_start
                  and o.issued_at is not distinct from s.issued_at
                order by o.ingested_at desc
                limit 1
            ) l on true
            where l.interval_end is null
               or l.value is distinct from s.value
               or l.interval_end <> s.interval_end
               or (l.quality = 'ok' and s.quality = 'quarantined')
            """,
            (run_id,),
        )
        return cur.rowcount


FactValue = float | str | None


@dataclass(frozen=True)
class FactRow:
    """One core.fact row (ADR 0003). `value` None means the source published an explicit null; it is
    stored as a JSON null in value_json (core.fact needs exactly one non-null value column), so a value
    that becomes null supersedes the number before it and stays distinct from 0."""

    entity_id: str
    attribute: str  # codebook entry ID
    value: FactValue
    unit: str | None
    valid_from: datetime | None
    known_at: datetime
    source_document_id: str
    run_id: uuid.UUID | None
    sensitivity: Sensitivity = "public"


@dataclass(frozen=True)
class StoredFact:
    entity_id: str
    attribute: str
    valid_from: datetime | None
    known_at: datetime
    fact_id: int
    value: Any


_EARLIEST = datetime.min.replace(tzinfo=UTC)


def _valid_key(t: datetime | None) -> datetime:
    return _EARLIEST if t is None else t


def select_new_facts(candidates: Sequence[FactRow], existing: Sequence[StoredFact]) -> list[FactRow]:
    """Candidates whose value differs from the latest known fact for the same (entity, attribute) at their
    valid time: among stored facts and earlier candidates with valid_from at or before the candidate's,
    the one with the latest valid_from, then the latest knowledge. A republication with an unchanged value
    writes nothing; a correction under the same valid_from writes a new row."""
    history: dict[tuple[str, str], list[tuple[datetime, datetime, int, Any]]] = {}
    for f in existing:
        history.setdefault((f.entity_id, f.attribute), []).append(
            (_valid_key(f.valid_from), f.known_at, f.fact_id, f.value)
        )
    out: list[FactRow] = []
    pending_id = 2**62  # orders a candidate written in this batch after every stored fact
    for c in sorted(candidates, key=lambda c: (c.entity_id, c.attribute, _valid_key(c.valid_from))):
        h = history.setdefault((c.entity_id, c.attribute), [])
        prior = [x for x in h if x[0] <= _valid_key(c.valid_from)]
        latest = max(prior, key=lambda x: (x[0], x[1], x[2]), default=None)
        if latest is None or latest[3] != c.value:  # None (published null) never equals 0.0
            out.append(c)
            pending_id += 1
            h.append((_valid_key(c.valid_from), c.known_at, pending_id, c.value))
    return out


def _stored_value(num: float | None, text: str | None, js: Any) -> Any:
    if num is not None:
        return float(num)
    if text is not None:
        return text
    return js  # None for a JSON null


def write_facts(conn: psycopg.Connection[Any], rows: Sequence[FactRow]) -> int:
    """Append the facts that are new or changed (select_new_facts). Returns rows written.

    Runs in a transaction block (a savepoint inside a caller's transaction); does not commit itself.
    """
    seen: set[tuple[str, str, datetime | None]] = set()
    for r in rows:
        key = (r.entity_id, r.attribute, r.valid_from)
        if key in seen:
            raise DuplicateKeyError(f"duplicate fact key in batch: {key}")
        seen.add(key)
    if not rows:
        return 0
    entities = sorted({r.entity_id for r in rows})
    attributes = sorted({r.attribute for r in rows})
    with conn.transaction():
        stored = [
            StoredFact(e, a, vf, ka, fid, _stored_value(num, text, js))
            for e, a, vf, ka, fid, num, text, js in conn.execute(
                "select entity_id, attribute, valid_from, known_at, fact_id, value_num, value_text, value_json "
                "from core.fact where entity_id = any(%s) and attribute = any(%s)",
                (entities, attributes),
            ).fetchall()
        ]
        new = select_new_facts(rows, stored)
        with conn.cursor() as cur:
            cur.executemany(
                """
                insert into core.fact (entity_id, attribute, value_num, value_text, value_json, unit, valid_from,
                                       known_at, source_document_id, run_id, sensitivity)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        r.entity_id,
                        r.attribute,
                        r.value if isinstance(r.value, float) else None,
                        r.value if isinstance(r.value, str) else None,
                        Jsonb(None) if r.value is None else None,
                        r.unit,
                        r.valid_from,
                        r.known_at,
                        r.source_document_id,
                        r.run_id,
                        r.sensitivity,
                    )
                    for r in new
                ],
            )
    return len(new)


def record_source_document(
    conn: psycopg.Connection[Any],
    *,
    document_id: str,
    source: str,
    url: str | None,
    title: str | None,
    fetched_at: datetime,
    sha256: str,
    raw_uri: str,
    content_type: str,
    sensitivity: Sensitivity = "public",
) -> None:
    """Insert a core.source_document row for a capture; the caller commits."""
    conn.execute(
        """
        insert into core.source_document (document_id, source, url, title, fetched_at, sha256, raw_uri, content_type, sensitivity)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (document_id, source, url, title, fetched_at, sha256, raw_uri, content_type, sensitivity),
    )


def lock_dataset(conn: psycopg.Connection[Any], source: str, dataset: str) -> None:
    """Wait for the session lock that serialises ingest runs of one dataset, and hold it until the connection
    closes. Runs of one dataset overlap through lookback and lookahead, and the orchestrator does not keep
    them apart (a backfill next to a schedule tick): without the lock a value fetched earlier could be written
    after a later revision and become the latest version, or an ok version could follow a quarantined one."""
    conn.execute("select pg_advisory_lock(hashtextextended(%s, 0))", (f"ingest:{source}:{dataset}",))
    conn.commit()


def start_run(
    conn: psycopg.Connection[Any],
    *,
    source: str,
    dataset: str,
    partition_key: str | None,
    window_start: datetime,
    window_end: datetime,
    code_version: str | None,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    conn.execute(
        """
        insert into ops.ingest_run (run_id, source, dataset, partition_key, window_start, window_end, status, code_version)
        values (%s, %s, %s, %s, %s, %s, 'running', %s)
        """,
        (run_id, source, dataset, partition_key, window_start, window_end, code_version),
    )
    conn.commit()
    return run_id


def finish_run(
    conn: psycopg.Connection[Any],
    run_id: uuid.UUID,
    *,
    status: str,
    rows_received: int | None,
    rows_expected: int | None,
    rows_written: int | None,
    raw_uris: list[str],
    error: str | None = None,
) -> None:
    """Close a run. finished_at is clock_timestamp(): now() would be the start of the transaction the update
    joins, which can be older than the run's last work."""
    conn.execute(
        """
        update ops.ingest_run
        set finished_at = clock_timestamp(), status = %s, rows_received = %s, rows_expected = %s, rows_written = %s,
            raw_uris = %s, error = %s
        where run_id = %s
        """,
        (status, rows_received, rows_expected, rows_written, raw_uris, error, run_id),
    )
    conn.commit()


def record_fingerprint(
    conn: psycopg.Connection[Any], run_id: uuid.UUID, fp: Fingerprint, devs: list[Deviation], sev: Severity
) -> None:
    conn.execute(
        "insert into ops.fingerprint (run_id, payload, deviations, severity) values (%s, %s, %s, %s)",
        (run_id, json.dumps(fp.to_json()), json.dumps([d.__dict__ for d in devs]), sev),
    )
    conn.commit()


def fingerprint_history(conn: psycopg.Connection[Any], source: str, dataset: str, limit: int = 60) -> list[Fingerprint]:
    rows = conn.execute(
        """
        select f.payload from ops.fingerprint f
        join ops.ingest_run r on r.run_id = f.run_id
        where r.source = %s and r.dataset = %s and r.status = 'ok'
        order by r.started_at desc limit %s
        """,
        (source, dataset, limit),
    ).fetchall()
    return [Fingerprint(**row[0]) for row in reversed(rows)]


def enqueue_bead(
    conn: psycopg.Connection[Any],
    *,
    kind: str,
    title: str,
    body: str,
    raw_uri: str | None,
    source: str | None,
    dataset: str | None,
) -> None:
    """Runtime to factory (ADR 0005). Never put sensitive content in title or body; link raw_uri."""
    conn.execute(
        """
        insert into ops.bead_outbox (kind, title, body, raw_uri, source, dataset)
        values (%s, %s, %s, %s, %s, %s)
        """,
        (kind, title, body, raw_uri, source, dataset),
    )
    conn.commit()


def last_snapshot_hash(conn: psycopg.Connection[Any], snapshot_id: str) -> str | None:
    row = conn.execute(
        "select sha256 from ops.snapshot where snapshot_id = %s and sha256 is not null "
        "order by fetched_at desc limit 1",
        (snapshot_id,),
    ).fetchone()
    return None if row is None else str(row[0])


def record_snapshot(
    conn: psycopg.Connection[Any],
    *,
    snapshot_id: str,
    url: str,
    status_code: int | None,
    sha256: str | None,
    raw_uri: str | None,
    changed: bool,
    error: str | None,
) -> None:
    conn.execute(
        """
        insert into ops.snapshot (snapshot_id, url, status_code, sha256, raw_uri, changed, error)
        values (%s, %s, %s, %s, %s, %s, %s)
        """,
        (snapshot_id, url, status_code, sha256, raw_uri, changed, error),
    )
    conn.commit()
