"""One-off rewrite of absolute raw-capture references to the store-relative form (scanner.core.raw_store).

References written before raw:// existed are absolute and differ by writer: file://C:/.../.data/raw/raw/...
from the host CLI, file:///data/raw/raw/... from the containers. `migrate` rewrites each one that is in the
store layout to raw://<source>/<dataset>/..., in every column that holds one (REFERENCES), in the caller's
transaction. References already relative are left alone, so a second run changes nothing; references not in
the layout are kept as they are and reported. Nothing is written unless `apply` is true (CLI
`raw-uris migrate`: a dry run without --yes; with it, one commit for every reference).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import psycopg

from scanner.core.raw_store import RawStore, relative_uri

# (table, reference column, key column for an array column)
REFERENCES: tuple[tuple[str, str, str | None], ...] = (
    ("ops.ingest_run", "raw_uris", "run_id"),
    ("ops.bead_outbox", "raw_uri", None),
    ("ops.snapshot", "raw_uri", None),
    ("core.source_document", "raw_uri", None),
)
_ROOT = re.compile(r"^(?P<root>.*)/raw/[^/]+/[^/]+/\d{4}/\d{2}/\d{2}/[^/]+$")


@dataclass
class ColumnReport:
    column: str
    references: int = 0
    relative: int = 0  # already raw://
    rewritten: int = 0
    unrecognised: list[str] = field(default_factory=list)
    roots: Counter[str] = field(default_factory=Counter)  # store roots of the rewritten references
    missing: list[str] = field(default_factory=list)  # references the store given to verify cannot read

    def text(self) -> str:
        roots = ", ".join(f"{r} ({n})" for r, n in self.roots.most_common()) or "none"
        lines = [
            f"{self.column}: {self.references} references, {self.relative} already relative, "
            f"{self.rewritten} to rewrite, {len(self.unrecognised)} not in the store layout (kept); roots: {roots}"
        ]
        lines += [f"  not in the store layout: {u}" for u in self.unrecognised[:5]]
        if self.missing:
            lines.append(f"  {len(self.missing)} not found in RAW_STORE_URL, e.g. {', '.join(self.missing[:3])}")
        return "\n".join(lines)


def _relative(uri: str, report: ColumnReport, exists: Callable[[str], bool] | None) -> str:
    report.references += 1
    rel = relative_uri(uri)
    if rel is None:
        report.unrecognised.append(uri)
        return uri
    if rel == uri:
        report.relative += 1
    else:
        report.rewritten += 1
        m = _ROOT.match(uri)
        report.roots[m["root"] if m else "?"] += 1
    if exists is not None and not exists(rel):
        report.missing.append(rel)
    return rel


def _exists_in(store: RawStore) -> Callable[[str], bool]:
    def exists(rel: str) -> bool:
        path = store.resolve(rel)
        return bool(store.fs.exists(path) or store.fs.exists(path + ".meta.json"))

    return exists


def migrate(conn: psycopg.Connection[Any], *, apply: bool, verify: RawStore | None = None) -> list[ColumnReport]:
    """Count, and with apply=True rewrite, the absolute references; the caller commits. With `verify`, also
    check that every reference in the store layout resolves to a capture (or its sidecar) in that store."""
    exists = None if verify is None else _exists_in(verify)
    reports = []
    for table, column, key in REFERENCES:
        report = ColumnReport(f"{table}.{column}")
        reports.append(report)
        if key is None:
            values = [v for (v,) in conn.execute(f"select {column} from {table} where {column} is not null")]
            changes = {v: new for v in values if (new := _relative(v, report, exists)) != v}
            params = [(new, old) for old, new in changes.items()]
            sql = f"update {table} set {column} = %s where {column} = %s"
        else:
            rows = conn.execute(f"select {key}, {column} from {table}").fetchall()
            params = [(new, k) for k, uris in rows if (new := [_relative(u, report, exists) for u in uris]) != uris]
            sql = f"update {table} set {column} = %s where {key} = %s"
        if apply and params:
            with conn.cursor() as cur:
                cur.executemany(sql, params)
    return reports
