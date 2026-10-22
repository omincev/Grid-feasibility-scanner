"""Decode ENTSO-E Transparency Platform XML (IEC 62325) into flat records.

Pure functions, no I/O. Spec: docs/sources/eu/entsoe.md, section "Decoding". In short:

- Publication_MarketDocument (prices, flows, capacities) and GL_MarketDocument (load, generation) carry
  TimeSeries; each Period has its own UTC timeInterval and resolution, so one document can mix
  resolutions (DK1 day-ahead: PT60M to 2025-09-30, PT15M from 2025-10-01).
- Point `position` p covers [period start + (p - 1) * resolution, + resolution).
- curveType A01 (fixed blocks): one Point per position. A position the source leaves out yields no
  record (a gap stays a gap).
- curveType A03 (variable-sized blocks): a Point's value holds from its position up to the next
  Point's position, or to the Period end. Expanding a block into one record per position decodes
  the format; it is not imputation, because the source states the value for every position in the
  block. Gaps in A03 are separate Periods, never omitted positions. Flagged for human sign-off.
- A Point without a value element means the source marks the value as not available: the record
  keeps value None (for A03, the whole block).
- Reason codes on a TimeSeries or Point and the GL `cancelledTS` flag are kept on the record, never
  acted on here; the adapter decides what they mean for a series.
- Acknowledgement_MarketDocument (no data, rejected query) raises AcknowledgementError.

Element names follow the XSDs iec62325-451-3-publication_v7_3, iec62325-451-6-generationload_v3_0 and
iec62325-451-1-acknowledgement_v7_0 (ENTSO-E transparency XSD repository, see the dossier).

Anything else unexpected raises ParseError: unknown root, curve type or Point element; duplicate or
out-of-range positions; a Period whose span is not a whole number of resolutions; a timestamp
without a UTC designator; DTDs or entity declarations; zipped payloads.
"""

from __future__ import annotations

import itertools
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from scanner.adapters.base import ParseError
from scanner.core.time import TimeConventionError, parse_duration

TIME_SERIES_DOCUMENTS = frozenset({"Publication_MarketDocument", "GL_MarketDocument"})
ACKNOWLEDGEMENT_DOCUMENT = "Acknowledgement_MarketDocument"
CURVE_FIXED_BLOCKS = "A01"
CURVE_VARIABLE_BLOCKS = "A03"

# TimeSeries header elements kept on every record: XML local name -> record field.
HEADER_FIELDS = {
    "businessType": "business_type",
    "in_Domain.mRID": "in_domain",
    "out_Domain.mRID": "out_domain",
    "inBiddingZone_Domain.mRID": "in_bidding_zone",
    "outBiddingZone_Domain.mRID": "out_bidding_zone",
    "contract_MarketAgreement.type": "contract_type",
    "auction.type": "auction_type",
    "classificationSequence_AttributeInstanceComponent.position": "classification_sequence",
    "objectAggregation": "object_aggregation",
    "currency_Unit.name": "currency",
    "price_Measure_Unit.name": "price_unit",
    "quantity_Measure_Unit.name": "quantity_unit",
    "cancelledTS": "cancelled_ts",
}
# Point value elements: XML local name -> record field. Codebook entries name the record field.
VALUE_FIELDS = {"quantity": "quantity", "secondaryQuantity": "secondary_quantity", "price.amount": "price_amount"}

_FORBIDDEN_MARKUP = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
_ZIP_MAGIC = b"PK\x03\x04"


@dataclass(frozen=True)
class Reason:
    code: str
    text: str | None


class AcknowledgementError(Exception):
    """The API answered with an Acknowledgement_MarketDocument instead of data.

    Reason code 999 is used both for "No matching data found" (HTTP 200) and for invalid queries
    (HTTP 400), so callers combine `no_matching_data` with the HTTP status. Not a ParseError: an
    empty answer for a day that is not published yet is normal.
    """

    def __init__(self, reasons: tuple[Reason, ...]):
        self.reasons = reasons
        detail = "; ".join(f"{r.code}: {r.text}" for r in reasons) or "no reason given"
        super().__init__(f"acknowledgement: {detail}")

    @property
    def no_matching_data(self) -> bool:
        return any(r.code == "999" and "no matching data" in (r.text or "").lower() for r in self.reasons)


@dataclass(frozen=True)
class Record:
    """One position of one Period of one TimeSeries, in UTC, with the TimeSeries dimensions."""

    document_type: str
    process_type: str | None
    time_series: str | None  # TimeSeries mRID: an index within the document, not a stable key
    business_type: str | None
    psr_type: str | None
    in_domain: str | None
    out_domain: str | None
    in_bidding_zone: str | None
    out_bidding_zone: str | None
    contract_type: str | None
    auction_type: str | None
    classification_sequence: str | None
    object_aggregation: str | None
    currency: str | None
    price_unit: str | None
    quantity_unit: str | None
    cancelled_ts: str | None  # GL documents: "true" marks a cancelled TimeSeries
    curve_type: str
    resolution: str
    interval_start: datetime
    interval_end: datetime
    position: int
    source_position: int  # the Point the value came from; differs from position inside an A03 block
    quantity: float | None
    secondary_quantity: float | None
    price_amount: float | None
    reason_codes: str | None  # sorted, comma-separated Reason codes of the TimeSeries and the Point

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly form with a fixed key set (fingerprints treat added or removed keys as hard).

        Positions are left out: they depend on how the source splits Periods (gaps, request window),
        and their ranges would read as value drift in the fingerprint.
        """
        d = asdict(self)
        del d["position"], d["source_position"]
        d["interval_start"] = _iso_z(self.interval_start)
        d["interval_end"] = _iso_z(self.interval_end)
        return d


def decode(content: bytes) -> list[Record]:
    """Decode one API response body. Raises AcknowledgementError or ParseError."""
    root = _parse(content)
    name = _local(root.tag)
    if name == ACKNOWLEDGEMENT_DOCUMENT:
        raise AcknowledgementError(_reasons(root))
    if name not in TIME_SERIES_DOCUMENTS:
        raise ParseError(f"unsupported document {name!r}")
    document_type = _required_text(root, "type")
    process = _child(root, "process.processType")
    process_type = _clean(process.text) if process is not None else None
    records: list[Record] = []
    for ts in _children(root, "TimeSeries"):
        records.extend(_time_series(ts, document_type, process_type))
    return records


def _parse(content: bytes) -> ET.Element:
    if content.startswith(_ZIP_MAGIC):
        raise ParseError("response is a zip archive; no supported dataset returns one (see dossier)")
    if _FORBIDDEN_MARKUP.search(content):
        raise ParseError("document declares a DTD or entities; refusing to parse")
    try:
        return ET.fromstring(content)
    except ET.ParseError as exc:
        raise ParseError(f"response is not well-formed XML: {exc}") from exc


def _time_series(ts: ET.Element, document_type: str, process_type: str | None) -> list[Record]:
    header: dict[str, str | None] = dict.fromkeys(HEADER_FIELDS.values())
    for child in ts:
        field = HEADER_FIELDS.get(_local(child.tag))
        if field is not None:
            header[field] = _clean(child.text)
    psr = _child(ts, "MktPSRType")
    psr_type = _clean(_text(psr, "psrType")) if psr is not None else None
    curve = _clean(_text(ts, "curveType")) or CURVE_FIXED_BLOCKS  # A01 is the default per IEC 62325
    if curve not in (CURVE_FIXED_BLOCKS, CURVE_VARIABLE_BLOCKS):
        raise ParseError(f"unsupported curveType {curve!r}")
    mrid = _clean(_text(ts, "mRID"))
    ts_reasons = _reason_codes(_children(ts, "Reason"))

    records: list[Record] = []
    for period in _children(ts, "Period"):
        start, resolution_text, resolution, n = _period_frame(period)
        for position, point in _expand(_points(period, n), n, curve):
            t0 = start + (position - 1) * resolution
            records.append(
                Record(
                    document_type=document_type,
                    process_type=process_type,
                    time_series=mrid,
                    psr_type=psr_type,
                    curve_type=curve,
                    resolution=resolution_text,
                    interval_start=t0,
                    interval_end=t0 + resolution,
                    position=position,
                    source_position=point.position,
                    reason_codes=",".join(sorted(ts_reasons | point.reasons)) or None,
                    **point.values,  # type: ignore[arg-type]
                    **header,  # type: ignore[arg-type]
                )
            )
    return records


def _period_frame(period: ET.Element) -> tuple[datetime, str, timedelta, int]:
    """Period start, resolution as published and parsed, and the number of positions it holds."""
    interval = _child(period, "timeInterval")
    if interval is None:
        raise ParseError("Period without timeInterval")
    start = _utc(_required_text(interval, "start"))
    end = _utc(_required_text(interval, "end"))
    resolution_text = _required_text(period, "resolution")
    try:
        resolution = parse_duration(resolution_text)
    except TimeConventionError as exc:
        raise ParseError(f"unsupported resolution {resolution_text!r}") from exc
    if resolution <= timedelta(0) or end <= start or (end - start) % resolution:
        raise ParseError(f"Period {start.isoformat()}/{end.isoformat()} is not a whole number of {resolution_text}")
    return start, resolution_text, resolution, (end - start) // resolution


@dataclass(frozen=True)
class _Point:
    position: int
    values: dict[str, float | None]
    reasons: frozenset[str]


def _points(period: ET.Element, n: int) -> list[_Point]:
    points: list[_Point] = []
    for el in _children(period, "Point"):
        raw = _required_text(el, "position")
        try:
            position = int(raw)
        except ValueError as exc:
            raise ParseError(f"position {raw!r} is not an integer") from exc
        if not 1 <= position <= n:
            raise ParseError(f"position {position} outside 1..{n}")
        values: dict[str, float | None] = dict.fromkeys(VALUE_FIELDS.values())
        for child in el:
            tag = _local(child.tag)
            if tag in ("position", "Reason"):
                continue
            if tag not in VALUE_FIELDS:
                raise ParseError(f"unexpected Point element {tag!r}")
            values[VALUE_FIELDS[tag]] = _number(child.text, tag)
        points.append(_Point(position, values, _reason_codes(_children(el, "Reason"))))
    points.sort(key=lambda p: p.position)
    for a, b in itertools.pairwise(points):
        if a.position == b.position:
            raise ParseError(f"duplicate position {a.position}")
    return points


def _expand(points: list[_Point], n: int, curve: str) -> list[tuple[int, _Point]]:
    """(position, source Point) for every position the document gives a value for."""
    if curve == CURVE_FIXED_BLOCKS or not points:
        return [(p.position, p) for p in points]
    ends = [p.position for p in points[1:]] + [n + 1]
    return [(q, p) for p, stop in zip(points, ends, strict=True) for q in range(p.position, stop)]


def _reason_codes(reasons: list[ET.Element]) -> frozenset[str]:
    return frozenset(code for r in reasons if (code := _clean(_text(r, "code"))))


def _reasons(root: ET.Element) -> tuple[Reason, ...]:
    reasons = []
    for el in root.iter():
        if _local(el.tag) == "Reason":
            reasons.append(Reason(code=_clean(_text(el, "code")) or "", text=_clean(_text(el, "text"))))
    return tuple(reasons)


def _utc(value: str) -> datetime:
    """ENTSO-E timestamps are UTC with a 'Z' suffix, e.g. 2024-07-27T22:00Z."""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ParseError(f"bad timestamp {value!r}") from exc
    if dt.tzinfo is None or dt.utcoffset() != timedelta(0):
        raise ParseError(f"timestamp {value!r} is not explicitly UTC")
    return dt.astimezone(UTC)


def _number(text: str | None, tag: str) -> float:
    try:
        value = float((text or "").strip())
    except ValueError as exc:
        raise ParseError(f"{tag} {text!r} is not a number") from exc
    if not math.isfinite(value):
        raise ParseError(f"{tag} {text!r} is not finite")
    return value


def _iso_z(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(el: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in el if _local(c.tag) == name]


def _child(el: ET.Element, name: str) -> ET.Element | None:
    found = _children(el, name)
    return found[0] if found else None


def _text(el: ET.Element, name: str) -> str | None:
    child = _child(el, name)
    return child.text if child is not None else None


def _clean(text: str | None) -> str | None:
    text = (text or "").strip()
    return text or None


def _required_text(el: ET.Element, name: str) -> str:
    text = _clean(_text(el, name))
    if text is None:
        raise ParseError(f"{_local(el.tag)} without {name}")
    return text
