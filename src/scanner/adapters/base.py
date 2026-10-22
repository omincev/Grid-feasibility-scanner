"""The adapter contract. Every source implements fetch, records and parse.

Rules (AGENTS.md): no imputation, no unit coercion, UTC only, explicit nulls kept as null, and a
ParseError for anything unexpected. Adapters never write to the database; the ingest runtime does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from scanner.core.codebook import CodebookEntry
from scanner.core.db import ObservationRow
from scanner.core.manifest import Manifest
from scanner.core.registry import Country


class ParseError(ValueError):
    """Source content does not match the manifest and codebook. Never caught and ignored."""


@dataclass(frozen=True)
class FetchResult:
    url: str
    params: dict[str, Any]
    status: int
    content_type: str
    content: bytes
    fetched_at: datetime
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ext(self) -> str:
        if "json" in self.content_type:
            return "json"
        if "xml" in self.content_type:
            return "xml"
        if "pdf" in self.content_type:
            return "pdf"
        if "html" in self.content_type:
            return "html"
        return "bin"


@dataclass(frozen=True)
class SeriesInfo:
    series_key: str
    entry: CodebookEntry
    entity_id: str
    # The series' own resolution, for manifests whose resolution is per_period; else the manifest's.
    resolution: timedelta | None = None


@dataclass(frozen=True)
class ParsedBatch:
    rows: list[ObservationRow]
    series: dict[str, SeriesInfo]


class Adapter(Protocol):
    def fetch(self, manifest: Manifest, start: datetime, end: datetime) -> list[FetchResult]:
        """Fetch everything covering [start, end) in UTC. May fetch a wider window; parse filters."""
        ...

    def records(self, result: FetchResult) -> list[dict[str, Any]]:
        """Decode a fetch into flat records for fingerprinting."""
        ...

    def parse(
        self,
        manifest: Manifest,
        country: Country,
        entries: list[CodebookEntry],
        result: FetchResult,
        start: datetime,
        end: datetime,
    ) -> ParsedBatch:
        """Turn one fetch into canonical rows for intervals starting in [start, end)."""
        ...
