"""ENTSO-E Transparency Platform adapter, the country-agnostic base tier (ADR 0011, WP3.1).

Dossier: docs/sources/eu/entsoe.md. Manifests (`manifests/*.yaml`, country `eu`) take their zones from
the registry (`zones_from_registry`), keyed by EIC; the manifest `zone_field` names the record field that
carries the zone's EIC. Request parameters per dataset are in QUERIES.

- fetch: one GET per zone per chunk of at most MAX_SPAN (the API's one-year limit), sequential, at most
  one request per MIN_INTERVAL_S in this process (the API allows 400 per minute per token). The token
  comes from settings at fetch time and travels only in a header (`client.get_document`).
- backfill reuse: a window that ended at least REUSE_MIN_AGE ago is fetched as a wider window per zone
  (Query.reuse_span) and the response serves the following daily runs of the same process, as in the
  Energi Data Service adapter (`_retired_responses`). Each run still records the response it used (the raw
  store keeps its bytes once), so its raw capture rebuilds it: `parse` keeps only the run's window. Recent
  windows always hit the API.
- records: decoded XML as flat dicts; an Acknowledgement "No matching data found" is no records, any
  other Acknowledgement a ParseError.
- parse: one series per zone, codebook entry and resolution (ADR 0002: a resolution change, such as
  DK1 day-ahead PT60M to PT15M on 2025-10-01, starts a new series). Units are checked, never converted.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import SecretStr

from scanner.adapters.base import FetchResult, ParsedBatch, ParseError, SeriesInfo
from scanner.adapters.entsoe.client import get_document
from scanner.adapters.entsoe.decode import AcknowledgementError, Record, decode
from scanner.core.codebook import CodebookEntry
from scanner.core.db import ObservationRow
from scanner.core.manifest import Manifest
from scanner.core.registry import Country
from scanner.core.time import ensure_utc, format_duration, parse_duration
from scanner.settings import get_settings

MAX_SPAN = timedelta(days=365)
# 2 requests/s per process: even three parallel backfill processes stay under the API's 400/min/token.
MIN_INTERVAL_S = 0.5
_TIME_FMT = "%Y%m%d%H%M"
# Only windows that ended this long ago may be served from, or widened for, a reused response: ENTSO-E
# data are corrected after publication, so today and the recent past (scheduled runs) are fetched fresh.
REUSE_MIN_AGE = timedelta(days=7)


@dataclass(frozen=True)
class Query:
    """Fixed request parameters of a dataset, and the parameters that take the zone's EIC."""

    params: dict[str, str]
    zone_params: tuple[str, ...]
    # Width of the window fetched per zone for a past run; smaller where a day has many series, because
    # every run decodes the whole response it used.
    reuse_span: timedelta = timedelta(days=31)


# Parameter spelling as in ENTSO-E's Postman collection (names are case sensitive).
QUERIES: dict[str, Query] = {
    "EnergyPrices": Query({"documentType": "A44", "contract_MarketAgreement.type": "A01"}, ("in_Domain", "out_Domain")),
    "ActualTotalLoad": Query({"documentType": "A65", "processType": "A16"}, ("outBiddingZone_Domain",)),
    "ActualGenerationPerType": Query({"documentType": "A75", "processType": "A16"}, ("in_Domain",), timedelta(days=7)),
}

# Codebook unit -> record fields that must carry these values (the source's own unit codes).
UNITS: dict[str, dict[str, str]] = {
    "EUR/MWh": {"currency": "EUR", "price_unit": "MWH"},
    "MW": {"quantity_unit": "MAW"},
}

# A dimension value a record may leave out: ENTSO-E omits classificationSequence when a zone publishes
# one sequence. Verified on the recorded fixtures: DK1 (absent) and DE-LU (sequence 1) equal the Energi
# Data Service day-ahead prices; DE-LU sequence 2 is another auction (codebook entsoe.EnergyPrices).
IMPLIED_WHEN_ABSENT = {"classification_sequence": "1"}

# (dataset, zone EIC) -> (start, end, response) of the last wide response; see EntsoeAdapter.fetch.
_reusable_responses: dict[tuple[str, str], tuple[datetime, datetime, FetchResult]] = {}

_throttle_lock = threading.Lock()
_last_request = [0.0]


def _throttle(sleep: Callable[[float], None]) -> None:
    """At most one request per MIN_INTERVAL_S across this process's threads."""
    with _throttle_lock:
        wait = _last_request[0] + MIN_INTERVAL_S - time.monotonic()
        if wait > 0:
            sleep(wait)
        _last_request[0] = time.monotonic()


def chunks(start: datetime, end: datetime, span: timedelta = MAX_SPAN) -> list[tuple[datetime, datetime]]:
    """[start, end) cut into consecutive half-open pieces of at most `span`."""
    out = []
    t = start
    while t < end:
        out.append((t, min(t + span, end)))
        t += span
    return out


def series_key(manifest: Manifest, zone_id: str, entry: CodebookEntry, resolution: timedelta) -> str:
    """eu.entsoe.<dataset>.<zone id>.<field>[.<dimension suffix>].<resolution>, e.g.
    eu.entsoe.EnergyPrices.dk.zone.dk1.price_amount.pt15m. The full zone ID keeps zones of different
    countries apart; the resolution suffix makes each resolution its own series (ADR 0002)."""
    suffix = entry.id.split(".")[4:]
    res = format_duration(resolution).lower()
    return ".".join([manifest.country, manifest.source_short, manifest.dataset, zone_id, entry.field, *suffix, res])


def _matches(rec: dict[str, Any], dimensions: dict[str, str]) -> bool:
    for k, v in dimensions.items():
        got = rec.get(k)
        if got is None:
            got = IMPLIED_WHEN_ABSENT.get(k)
        if got != v:
            return False
    return True


def _requested_window(result: FetchResult) -> tuple[datetime, datetime]:
    """The chunk a response answers. Daily articles return whole days beyond it, so parsing keeps only
    this window and neighbouring chunks never yield the same interval twice."""
    try:
        start = datetime.strptime(str(result.params["periodStart"]), _TIME_FMT).replace(tzinfo=UTC)
        end = datetime.strptime(str(result.params["periodEnd"]), _TIME_FMT).replace(tzinfo=UTC)
    except (KeyError, ValueError) as exc:
        raise ParseError(f"response without a readable periodStart/periodEnd: {exc}") from exc
    return start, end


class EntsoeAdapter:
    def __init__(
        self,
        client: httpx.Client | None = None,
        token: SecretStr | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        s = get_settings()
        self.client = client or httpx.Client(timeout=s.http_timeout_s, headers={"User-Agent": s.user_agent})
        self._token = token
        self._sleep = sleep

    def _get_token(self) -> SecretStr:
        token = self._token or get_settings().entsoe_api_token
        if token is None or not token.get_secret_value():
            raise RuntimeError("ENTSOE_API_TOKEN (or ENTSOE_API_KEY) is not set")
        return token

    def fetch(self, manifest: Manifest, start: datetime, end: datetime) -> list[FetchResult]:
        query = QUERIES.get(manifest.dataset)
        if query is None:
            raise ValueError(f"no ENTSO-E query defined for dataset {manifest.dataset}")
        token = self._get_token()
        start, end = ensure_utc(start), ensure_utc(end)
        cutoff = datetime.now(UTC) - REUSE_MIN_AGE
        results: list[FetchResult] = []
        for eic in sorted(manifest.zones):
            if end > cutoff:  # recent: always fresh, never widened or cached
                results.extend(self._request(query, eic, a, b, token) for a, b in chunks(start, end))
                continue
            key = (manifest.dataset, eic)
            hit = _reusable_responses.get(key)
            if hit is not None and hit[0] <= start and end <= hit[1]:
                results.append(hit[2])
                continue
            # A daily run's window is a few days; fetch reuse_span (at most up to the cutoff) in one request.
            wide_end = max(end, min(start + query.reuse_span, cutoff))
            pieces = [self._request(query, eic, a, b, token) for a, b in chunks(start, wide_end)]
            if len(pieces) == 1 and 200 <= pieces[0].status < 300:
                _reusable_responses[key] = (start, wide_end, pieces[0])
            results.extend(pieces)
        return results

    def _request(self, query: Query, eic: str, a: datetime, b: datetime, token: SecretStr) -> FetchResult:
        params = {
            **query.params,
            **dict.fromkeys(query.zone_params, eic),
            "periodStart": a.strftime(_TIME_FMT),
            "periodEnd": b.strftime(_TIME_FMT),
        }
        _throttle(self._sleep)
        return get_document(self.client, params, token, sleep=self._sleep)

    def _decode(self, result: FetchResult) -> list[Record]:
        try:
            return decode(result.content)
        except AcknowledgementError as exc:
            if exc.no_matching_data:
                return []
            raise ParseError(str(exc)) from exc

    def records(self, result: FetchResult) -> list[dict[str, Any]]:
        return [r.as_dict() for r in self._decode(result)]

    def parse(
        self,
        manifest: Manifest,
        country: Country,
        entries: list[CodebookEntry],
        result: FetchResult,
        start: datetime,
        end: datetime,
    ) -> ParsedBatch:
        chunk_start, chunk_end = _requested_window(result)
        lo, hi = max(start, chunk_start), min(end, chunk_end)
        rows: list[ObservationRow] = []
        series: dict[str, SeriesInfo] = {}
        seen: dict[tuple[str, datetime], dict[str, Any]] = {}
        for rec in self._decode(result):
            if not lo <= rec.interval_start < hi or rec.cancelled_ts == "true":
                continue
            d = rec.as_dict()
            eic = d.get(manifest.zone_field) if manifest.zone_field else None
            zone_id = manifest.zones.get(eic) if eic else None
            if zone_id is None:
                continue
            resolution = parse_duration(rec.resolution)
            for entry in entries:
                if not _matches(d, entry.dimensions):
                    continue
                expected_units = UNITS.get(entry.unit)
                if expected_units is None:
                    raise ParseError(f"{entry.id}: no source unit mapping for {entry.unit}")
                if any(d.get(k) != v for k, v in expected_units.items()):
                    got = {k: d.get(k) for k in expected_units}
                    raise ParseError(
                        f"{entry.id}: source unit {got} at {rec.interval_start.isoformat()}, expected {expected_units}"
                    )
                if entry.field not in d:
                    raise ParseError(f"{entry.id}: record has no field {entry.field}")
                key = series_key(manifest, zone_id, entry, resolution)
                # The same TimeSeries may be published twice (differing only in its mRID, e.g. NO2 from
                # 2026-08-07). Identical repeats collapse; any difference is a hard error, never a pick.
                same = {k: v for k, v in d.items() if k != "time_series"}
                earlier = seen.get((key, rec.interval_start))
                if earlier is not None:
                    if earlier == same:
                        continue
                    raise ParseError(
                        f"two different values for {key} (zone {zone_id}) at {rec.interval_start.isoformat()}: "
                        f"{earlier.get(entry.field)} and {d[entry.field]}"
                        f"{'' if earlier.get(entry.field) != d[entry.field] else ' (same value, different headers)'}"
                    )
                seen[(key, rec.interval_start)] = same
                series.setdefault(key, SeriesInfo(key, entry, zone_id, resolution))
                rows.append(ObservationRow(key, rec.interval_start, rec.interval_end, None, d[entry.field]))
        return ParsedBatch(rows=rows, series=series)
