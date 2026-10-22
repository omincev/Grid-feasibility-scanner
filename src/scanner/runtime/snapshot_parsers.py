"""Parsers that snapshots with on_change `parse:<name>` run on each successful capture.

Each parser gets the capture and writes facts that changed (scanner.core.db.write_facts), citing the
capture as a core.source_document. Parsers run inside a transaction opened by the caller.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from scanner.adapters.energinet import connection_queue as cq
from scanner.core import db
from scanner.core.raw_store import RawRef
from scanner.core.registry import Snapshot


@dataclass(frozen=True)
class Capture:
    snapshot: Snapshot
    content: bytes
    content_type: str
    raw: RawRef
    fetched_at: datetime
    run_id: uuid.UUID


@dataclass(frozen=True)
class ParseOutcome:
    facts_written: int
    notes: list[str]


def _document(conn: psycopg.Connection[Any], cap: Capture, source: str) -> str:
    doc_id = f"snapshot:{cap.snapshot.id}:{cap.run_id}"
    db.record_source_document(
        conn,
        document_id=doc_id,
        source=source,
        url=cap.snapshot.url,
        title=cap.snapshot.id,
        fetched_at=cap.fetched_at,
        sha256=cap.raw.sha256,
        raw_uri=cap.raw.uri,
        content_type=cap.content_type or "application/octet-stream",
    )
    return doc_id


def connection_queue(conn: psycopg.Connection[Any], cap: Capture) -> ParseOutcome:
    """Queue facts per region (scanner.adapters.energinet.connection_queue)."""
    cells, valid_from = cq.queue_cells(json.loads(cap.content))
    notes = [] if valid_from else ["no 'Senest opdateret' date in the response: valid_from left null"]
    doc_id = _document(conn, cap, cq.SOURCE)
    rows = []
    for region in sorted({c.region for c in cells}):
        eid, parent = cq.region_entity(region)
        if region not in cq.REGION_ZONES:
            notes.append(f"region {region!r} has no zone mapping; parent set to {parent}")
        conn.execute(
            """
            insert into ref.entity (entity_id, kind, country_code, parent_id, name, attributes, sensitivity,
                                    source_path)
            values (%s, 'tso_area', %s, %s, %s, %s, 'public', %s)
            on conflict (entity_id) do update set parent_id = excluded.parent_id, name = excluded.name,
                attributes = excluded.attributes, source_path = excluded.source_path, loaded_at = now()
            """,
            (
                eid,
                cq.COUNTRY,
                parent,
                region,
                Jsonb({"source": cq.SOURCE, "source_name": region}),
                f"runtime:{cq.SOURCE}",
            ),
        )
    for c in cells:
        rows.append(
            db.FactRow(
                entity_id=cq.region_entity(c.region)[0],
                attribute=c.entry_id,
                value=c.mw,
                unit="MW",
                valid_from=valid_from,
                known_at=cap.fetched_at,
                source_document_id=doc_id,
                run_id=cap.run_id,
            )
        )
    return ParseOutcome(db.write_facts(conn, rows), notes)


PARSERS: dict[str, Callable[[psycopg.Connection[Any], Capture], ParseOutcome]] = {
    "connection_queue": connection_queue,
}
