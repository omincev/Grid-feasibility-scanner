"""Energi Data Service adapter (docs/sources/dk/energidataservice.md).

All datasets share one shape: {"total": n, "filters": ..., "sort": ..., "limit": ..., "dataset": ...,
"records": [{...}, ...]}, where `total` is the number of records returned, not the dataset size. The
manifest names the UTC time field and the zone field; codebook entries name value fields and dimensions.

API behaviour, verified live on 2026-10-18 (details and evidence in the dossier):
- `start` (inclusive) and `end` (exclusive) filter on the dataset's UTC time column and are read as
  Danish local time unless `timezone=UTC` is passed. OpenAPI spec
  https://api.energidataservice.dk/swagger/public/swagger.json: "DK (default) or UTC. For
  interpretation of fixed time values." API guide https://www.energidataservice.dk/guides/api-guides:
  "The date and time refer to the Danish timezone / local Danish time." This adapter sends UTC times
  with `timezone=UTC` and still filters on the UTC field when parsing.
- `limit=0` returns every matching row (spec: "0 returns all values"; without `start` and `limit` the
  API returns 100 rows). No page cap was observed: a month of ImbalancePrice (5,952 rows) came back in
  one response. `offset` exists and is not needed.
- Rate limits are per dataset and unpublished (API guide; news item 163, "Differentiated rate limits").
  Observed: 3 calls per ~10 s on active datasets (`totalcalls`/`remainingcalls` headers) and 1 call per
  ~5 minutes on the discontinued Elspotprices. Excess calls get HTTP 429 with a `Retry-After` header;
  this adapter waits that long (capped) and retries. For backfill_only (retired) datasets one request
  covers 31 days and serves the following daily partitions in the same process, so a 211-day
  Elspotprices backfill takes about 7 calls instead of 211 (about 18 hours at one call per 5 minutes).
- Bad parameters give 400 with a plain-text message, an unknown dataset 404. Those, and a 5xx or 429
  that outlives the retries, are returned to the runtime, which treats them as hard deviations.
"""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from tenacity import RetryCallState, Retrying, retry_if_exception_type, retry_if_result, stop_after_attempt
from tenacity import wait_exponential as _wait_exponential

from scanner.adapters.base import FetchResult, ParsedBatch, ParseError, SeriesInfo
from scanner.adapters.issued_at import resolve_issued_at
from scanner.core.codebook import CodebookEntry
from scanner.core.db import ObservationRow
from scanner.core.manifest import Manifest
from scanner.core.registry import Country
from scanner.core.time import ensure_utc, parse_duration, parse_source_utc
from scanner.settings import get_settings

BASE_URL = "https://api.energidataservice.dk/dataset"
ATTEMPTS = 4
# Discontinued datasets allow one call per ~5 minutes; waiting longer than that means something else is wrong.
MAX_RETRY_AFTER_S = 330.0
_backoff = _wait_exponential(multiplier=2, max=60)
# Backfill_only (retired) datasets: one request covers this span; see EnergiDataServiceAdapter.fetch.
RETIRED_SPAN = timedelta(days=31)
# Active datasets, only when EDS_HISTORY_SPAN_DAYS is set (a backfill: `compose run --rm -e
# EDS_HISTORY_SPAN_DAYS=366 cli ingest ...`): days older than the lookback share one request of that many days,
# cut where the lookback window begins. Some datasets allow about one call per minute or two (FcrNdDK2), so one
# request per day would take days; the schedules no longer re-fetch those days. Off by default.
HISTORY_SPAN_ENV = "EDS_HISTORY_SPAN_DAYS"
_retired_responses: dict[str, tuple[datetime, datetime, FetchResult]] = {}


def series_key(manifest: Manifest, zone_id: str, entry: CodebookEntry) -> str:
    """<cc>.<source_short>.<dataset>.<zone slug>.<field>[.<dimension suffix>]"""
    zone_slug = zone_id.split(".")[-1]
    suffix = entry.id.split(".")[4:]  # dimension suffix from the codebook id, if any
    parts = [manifest.country, manifest.source_short, manifest.dataset, zone_slug, entry.field, *suffix]
    return ".".join(parts)


def _api_time(t: datetime) -> str:
    return ensure_utc(t).strftime("%Y-%m-%dT%H:%M")


def _retryable(resp: httpx.Response) -> bool:
    return resp.status_code == 429 or resp.status_code >= 500


def _wait(state: RetryCallState) -> float:
    """Honour Retry-After on a 429; otherwise exponential backoff."""
    outcome = state.outcome
    if outcome is not None and not outcome.failed:
        retry_after = outcome.result().headers.get("retry-after", "")
        if retry_after.isdigit():
            return min(float(retry_after) + 1.0, MAX_RETRY_AFTER_S)
    return float(_backoff(state))


def _final_outcome(state: RetryCallState) -> httpx.Response:
    """After the last attempt, return a 5xx/429 response so the runtime stores and fingerprints it.

    A transport error has no response to store; `result()` re-raises it.
    """
    assert state.outcome is not None
    resp: httpx.Response = state.outcome.result()
    return resp


class EnergiDataServiceAdapter:
    def __init__(self, client: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep):
        s = get_settings()
        self.client = client or httpx.Client(timeout=s.http_timeout_s, headers={"User-Agent": s.user_agent})
        self._sleep = sleep

    def _get(self, url: str, params: dict[str, Any]) -> httpx.Response:
        retrying = Retrying(
            retry=retry_if_exception_type(httpx.TransportError) | retry_if_result(_retryable),
            wait=_wait,
            stop=stop_after_attempt(ATTEMPTS),
            sleep=self._sleep,
            retry_error_callback=_final_outcome,
        )
        resp: httpx.Response = retrying(self.client.get, url, params=params)
        return resp

    def fetch(self, manifest: Manifest, start: datetime, end: datetime) -> list[FetchResult]:
        # A retired dataset no longer changes and allows one call per ~5 minutes, so one response
        # spanning RETIRED_SPAN serves the following daily partitions of a backfill in this process. An active
        # dataset does the same, when EDS_HISTORY_SPAN_DAYS is set, for windows that end before its lookback;
        # recent windows are always requested on their own. Each run still records the response it used (the raw
        # store keeps its bytes once); parsing keeps only the requested window.
        cutoff: datetime | None = None
        span = RETIRED_SPAN
        if manifest.status != "backfill_only":
            history_days = int(os.environ.get(HISTORY_SPAN_ENV) or 0)
            cutoff = datetime.now(UTC) - parse_duration(manifest.lookback) - timedelta(days=1)
            if history_days <= 0 or end > cutoff:
                return [self._request(manifest, start, end)]
            span = timedelta(days=history_days)
        key = f"{manifest.dataset}:{sorted(manifest.zones)}"
        hit = _retired_responses.get(key)
        if hit is not None and hit[0] <= start and end <= hit[1]:
            return [hit[2]]
        span_end = max(end, start + span)
        if cutoff is not None:
            span_end = max(end, min(span_end, cutoff))
        result = self._request(manifest, start, span_end)
        if 200 <= result.status < 300:
            _retired_responses[key] = (start, span_end, result)
        return [result]

    def _request(self, manifest: Manifest, start: datetime, end: datetime) -> FetchResult:
        params: dict[str, Any] = {
            "start": _api_time(start),
            "end": _api_time(end),
            "timezone": "UTC",
            "sort": f"{manifest.time_field_utc} asc",
            "limit": 0,
        }
        if manifest.zone_field:
            params["filter"] = json.dumps({manifest.zone_field: sorted(manifest.zones)})
        url = f"{BASE_URL}/{manifest.dataset}"
        resp = self._get(url, params)
        return FetchResult(
            url=str(resp.request.url),
            params=params,
            status=resp.status_code,
            content_type=resp.headers.get("content-type", ""),
            content=resp.content,
            fetched_at=datetime.now(UTC),
            headers={
                k: v
                for k, v in resp.headers.items()
                if k.lower() in ("content-type", "date", "etag", "retry-after", "totalcalls", "remainingcalls")
            },
        )

    def records(self, result: FetchResult) -> list[dict[str, Any]]:
        try:
            body = json.loads(result.content)
        except json.JSONDecodeError as exc:
            raise ParseError(f"response is not JSON: {exc}") from exc
        recs = body.get("records") if isinstance(body, dict) else None
        if not isinstance(recs, list):
            raise ParseError("response has no 'records' list")
        return recs

    def parse(
        self,
        manifest: Manifest,
        country: Country,
        entries: list[CodebookEntry],
        result: FetchResult,
        start: datetime,
        end: datetime,
    ) -> ParsedBatch:
        resolution = manifest.resolution_td
        rows: list[ObservationRow] = []
        series: dict[str, SeriesInfo] = {}
        for rec in self.records(result):
            if manifest.time_field_utc not in rec:
                raise ParseError(f"record without {manifest.time_field_utc}: {rec}")
            t0 = parse_source_utc(str(rec[manifest.time_field_utc]))
            if not (start <= t0 < end):
                continue
            if t0.timestamp() % resolution.total_seconds():
                # The source documents full-resolution stamps. An off-grid start (seen in FcrNdDK2 on
                # 2025-10-26, where the hour after the DST change is stamped 01:01Z) would be stored as a
                # wrong, overlapping interval, so the day is a hard deviation instead.
                raise ParseError(f"{manifest.time_field_utc} {t0.isoformat()} is not on the {manifest.resolution} grid")
            if manifest.zone_field:
                code = rec.get(manifest.zone_field)
                if code not in manifest.zones:
                    continue
                zone_id = manifest.zones[code]
            else:
                (zone_id,) = manifest.zones.values()
            for entry in entries:
                if any(rec.get(k) != v for k, v in entry.dimensions.items()):
                    continue
                try:
                    issued_at = resolve_issued_at(entry.issued_at, interval_start=t0, record=rec, tz=country.timezone)
                except ValueError as exc:
                    raise ParseError(f"{entry.id} at {t0.isoformat()}: {exc}") from exc
                if issued_at is not None and issued_at > result.fetched_at:
                    # Not issued yet when fetched (e.g. tomorrow's intraday forecast): whatever the
                    # field holds is not that vintage, so it is not stored.
                    continue
                if entry.field not in rec:
                    raise ParseError(f"field {entry.field} missing in record at {t0.isoformat()}")
                value = rec[entry.field]
                if value is not None:
                    if isinstance(value, bool) or not isinstance(value, int | float):
                        raise ParseError(f"{entry.field} is {type(value).__name__}, expected number or null")
                    value = float(value)
                    if not math.isfinite(value):
                        raise ParseError(f"{entry.field} is not finite at {t0.isoformat()}")
                key = series_key(manifest, zone_id, entry)
                series.setdefault(key, SeriesInfo(series_key=key, entry=entry, entity_id=zone_id))
                rows.append(
                    ObservationRow(
                        series_key=key,
                        interval_start=t0,
                        interval_end=t0 + resolution,
                        issued_at=issued_at,
                        value=value,
                    )
                )
        return ParsedBatch(rows=rows, series=series)
