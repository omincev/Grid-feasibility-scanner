"""ENTSO-E XML decoding (src/scanner/adapters/entsoe/decode.py).

Two kinds of input:
- `postman__*.xml` in tests/fixtures/entsoe: example responses from ENTSO-E's official API documentation
  (Postman collection); provenance in each `.meta.json`.
- Inline documents built by `doc()` below: SYNTHETIC, hand-written to the same schema to pin down DST
  days, the 2025-10-01 resolution switch, A03 blocks, gaps and malformed input. Values are made up.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest
from conftest import FIXTURES
from hypothesis import given
from hypothesis import strategies as st

from scanner.adapters.base import ParseError
from scanner.adapters.entsoe import AcknowledgementError, Record, decode
from scanner.core.time import expected_count, local_day_bounds

DK1 = "10YDK-1--------W"
PUB_NS = "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3"


def _fixture(name: str) -> bytes:
    return (FIXTURES / "entsoe" / name).read_bytes()


def period(start: str, end: str, resolution: str, points: list[tuple[int, float | None]]) -> str:
    """SYNTHETIC Period. A None value writes a Point without price.amount (value not available)."""
    pts = "".join(
        f"<Point><position>{p}</position>{'' if v is None else f'<price.amount>{v}</price.amount>'}</Point>"
        for p, v in points
    )
    return (
        f"<Period><timeInterval><start>{start}</start><end>{end}</end></timeInterval>"
        f"<resolution>{resolution}</resolution>{pts}</Period>"
    )


def series(periods: list[str], curve: str | None = "A03", extra: str = "") -> str:
    curve_el = f"<curveType>{curve}</curveType>" if curve else ""
    return (
        "<TimeSeries><mRID>1</mRID><auction.type>A01</auction.type><businessType>A62</businessType>"
        f'<in_Domain.mRID codingScheme="A01">{DK1}</in_Domain.mRID>'
        f'<out_Domain.mRID codingScheme="A01">{DK1}</out_Domain.mRID>'
        "<contract_MarketAgreement.type>A01</contract_MarketAgreement.type>"
        "<currency_Unit.name>EUR</currency_Unit.name><price_Measure_Unit.name>MWH</price_Measure_Unit.name>"
        f"{extra}{curve_el}{''.join(periods)}</TimeSeries>"
    )


def doc(*time_series: str, root: str = "Publication_MarketDocument", doc_type: str = "A44") -> bytes:
    """SYNTHETIC document wrapper."""
    return (
        f'<?xml version="1.0" encoding="utf-8"?><{root} xmlns="{PUB_NS}"><mRID>synthetic</mRID>'
        f"<revisionNumber>1</revisionNumber><type>{doc_type}</type>{''.join(time_series)}</{root}>"
    ).encode()


def _contiguous(records: list[Record]) -> bool:
    return all(a.interval_end == b.interval_start for a, b in pairwise(records))


# --- official documentation examples ---------------------------------------------------------------


def test_official_a44_a03_expands_omitted_position() -> None:
    recs = decode(_fixture("postman__A44_AT_2024-07-28.xml"))
    assert len(recs) == 96  # 95 Points in the XML; A03 leaves out one position that repeats its predecessor
    assert recs[0].interval_start == datetime(2024, 7, 27, 22, tzinfo=UTC)
    assert recs[-1].interval_end == datetime(2024, 7, 28, 22, tzinfo=UTC)
    assert _contiguous(recs)
    expanded = [r for r in recs if r.position != r.source_position]
    assert len(expanded) == 1
    (e,) = expanded
    assert e.price_amount == recs[e.source_position - 1].price_amount
    r = recs[0]
    assert (r.document_type, r.curve_type, r.resolution, r.classification_sequence) == ("A44", "A03", "PT15M", "2")
    assert (r.currency, r.price_unit, r.quantity, r.contract_type) == ("EUR", "MWH", None, "A01")


def test_official_a65_load() -> None:
    recs = decode(_fixture("postman__A65_CZ_2023-03-03.xml"))
    assert len(recs) == 72 and _contiguous(recs)
    assert {r.out_bidding_zone for r in recs} == {"10YCZ-CEPS-----N"}
    assert {(r.process_type, r.business_type, r.quantity_unit, r.resolution) for r in recs} == {
        ("A16", "A04", "MAW", "PT60M")
    }
    assert all(r.quantity is not None and r.price_amount is None for r in recs)


def test_official_a75_generation_and_consumption_series() -> None:
    recs = decode(_fixture("postman__A75_DE_2023-08-16.xml"))
    per_series = Counter((r.psr_type, r.in_bidding_zone, r.out_bidding_zone) for r in recs)
    assert set(per_series.values()) == {96}
    # Generation series name the zone as inBiddingZone_Domain, consumption series as outBiddingZone_Domain.
    assert ("B10", "10Y1001A1001A83F", None) in per_series
    assert ("B10", None, "10Y1001A1001A83F") in per_series
    assert ("B16", "10Y1001A1001A83F", None) in per_series


def test_official_a11_flow_direction() -> None:
    recs = decode(_fixture("postman__A11_DE-Amprion_BE_2023-08-24.xml"))
    assert len(recs) == 96
    assert {(r.out_domain, r.in_domain) for r in recs} == {("10YDE-RWENET---I", "10YBE----------2")}


def test_records_have_a_fixed_key_set() -> None:
    a = decode(_fixture("postman__A44_AT_2024-07-28.xml"))[0].as_dict()
    b = decode(_fixture("postman__A65_CZ_2023-03-03.xml"))[0].as_dict()
    assert list(a) == list(b)
    assert a["interval_start"] == "2024-07-27T22:00:00Z"


# --- synthetic: DST days, resolution switch, A03 semantics ------------------------------------------


@pytest.mark.parametrize(
    ("day", "expected"),
    [("2025-10-26", 100), ("2026-03-29", 92), ("2026-10-14", 96)],  # DST end, DST start, normal day
)
def test_dst_days_quarter_hour_counts(day: str, expected: int) -> None:
    d = datetime.fromisoformat(day).date()
    start, end = local_day_bounds(d, "Europe/Copenhagen")
    assert expected_count(d, "Europe/Copenhagen", timedelta(minutes=15)) == expected
    z = "%Y-%m-%dT%H:%MZ"
    # A03 with three blocks: positions 1-10, 11-50 and 51-end.
    body = doc(series([period(start.strftime(z), end.strftime(z), "PT15M", [(1, 10.0), (11, -5.5), (51, 0.0)])]))
    recs = decode(body)
    assert len(recs) == expected
    assert recs[0].interval_start == start and recs[-1].interval_end == end
    assert _contiguous(recs)
    assert [r.price_amount for r in recs[9:12]] == [10.0, -5.5, -5.5]
    assert recs[-1].price_amount == 0.0 and recs[-1].source_position == 51


def test_resolution_switch_2025_10_01() -> None:
    """Hourly on 2025-09-30, quarter-hourly from 2025-10-01: each Period keeps its own resolution."""
    hourly = period("2025-09-29T22:00Z", "2025-09-30T22:00Z", "PT60M", [(p, float(p)) for p in range(1, 25)])
    quarter = period("2025-09-30T22:00Z", "2025-10-01T22:00Z", "PT15M", [(p, float(p)) for p in range(1, 97)])
    recs = decode(doc(series([hourly], curve="A01"), series([quarter], curve="A01")))
    by_res = Counter(r.resolution for r in recs)
    assert by_res == {"PT60M": 24, "PT15M": 96}
    for r in recs:
        assert r.interval_end - r.interval_start == (
            timedelta(hours=1) if r.resolution == "PT60M" else timedelta(minutes=15)
        )
    assert _contiguous(recs)


def test_a03_gap_between_periods_stays_a_gap() -> None:
    first = period("2025-10-01T22:00Z", "2025-10-02T06:00Z", "PT60M", [(1, 1.0)])
    second = period("2025-10-02T08:00Z", "2025-10-02T22:00Z", "PT60M", [(1, 2.0), (3, 3.0)])
    recs = decode(doc(series([first, second])))
    assert len(recs) == 8 + 14
    starts = {r.interval_start for r in recs}
    assert datetime(2025, 10, 2, 6, tzinfo=UTC) not in starts
    assert datetime(2025, 10, 2, 7, tzinfo=UTC) not in starts
    assert [r.price_amount for r in recs[8:11]] == [2.0, 2.0, 3.0]


def test_point_without_value_is_explicit_null_for_its_block() -> None:
    p = period("2025-10-01T22:00Z", "2025-10-02T00:00Z", "PT15M", [(1, 7.0), (3, None), (6, 8.0)])
    recs = decode(doc(series([p])))
    assert [r.price_amount for r in recs] == [7.0, 7.0, None, None, None, 8.0, 8.0, 8.0]


def test_a01_omitted_position_yields_no_record() -> None:
    p = period("2025-10-01T22:00Z", "2025-10-01T23:00Z", "PT15M", [(1, 1.0), (2, 2.0), (4, 4.0)])
    recs = decode(doc(series([p], curve="A01")))
    assert [r.position for r in recs] == [1, 2, 4]


def test_missing_curve_type_means_a01() -> None:
    p = period("2025-10-01T22:00Z", "2025-10-01T23:00Z", "PT15M", [(1, 1.0), (4, 4.0)])
    recs = decode(doc(series([p], curve=None)))
    assert [(r.position, r.curve_type) for r in recs] == [(1, "A01"), (4, "A01")]


def test_a03_positions_before_the_first_point_have_no_value() -> None:
    p = period("2025-10-01T22:00Z", "2025-10-01T23:00Z", "PT15M", [(3, 5.0)])
    recs = decode(doc(series([p])))
    assert [(r.position, r.price_amount) for r in recs] == [(3, 5.0), (4, 5.0)]


def test_reason_codes_cancelled_flag_and_secondary_quantity_are_kept() -> None:
    p = period("2025-10-01T22:00Z", "2025-10-01T22:30Z", "PT15M", [(1, 1.0), (2, 2.0)])
    p = p.replace("<position>2</position>", "<position>2</position><Reason><code>B31</code></Reason>")
    ts = series([p], curve="A03", extra="<Reason><code>A95</code><text>synthetic</text></Reason>")
    recs = decode(doc(ts))
    assert [r.reason_codes for r in recs] == ["A95", "A95,B31"]
    gl = (
        series([period("2025-10-01T22:00Z", "2025-10-01T22:15Z", "PT15M", [(1, 3.0)])], curve="A01")
        .replace("price.amount", "quantity")
        .replace("<curveType>", "<cancelledTS>true</cancelledTS><curveType>")
        .replace("</quantity></Point>", "</quantity><secondaryQuantity>4.5</secondaryQuantity></Point>")
    )
    (r,) = decode(doc(gl, root="GL_MarketDocument", doc_type="A75"))
    assert (r.quantity, r.secondary_quantity, r.cancelled_ts) == (3.0, 4.5, "true")


def test_psr_type_dimension() -> None:
    p = period("2025-10-01T22:00Z", "2025-10-01T23:00Z", "PT60M", [(1, 12.0)])
    ts = series([p], curve="A01", extra="<MktPSRType><psrType>B19</psrType></MktPSRType>")
    (r,) = decode(doc(ts, root="GL_MarketDocument", doc_type="A75"))
    assert r.psr_type == "B19" and r.document_type == "A75"


@given(st.lists(st.integers(min_value=-3, max_value=3), min_size=1, max_size=40))
def test_a03_expansion_inverts_a03_compression(values: list[int]) -> None:
    """Property: dropping repeated values (A03) and decoding gives the same records as full A01."""
    n = len(values)
    end = (datetime(2025, 10, 1, 22, tzinfo=UTC) + n * timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%MZ")
    full = [(i + 1, float(v)) for i, v in enumerate(values)]
    compressed = [(p, v) for i, (p, v) in enumerate(full) if i == 0 or v != full[i - 1][1]]
    a01 = decode(doc(series([period("2025-10-01T22:00Z", end, "PT15M", full)], curve="A01")))
    a03 = decode(doc(series([period("2025-10-01T22:00Z", end, "PT15M", compressed)], curve="A03")))
    assert [(r.interval_start, r.price_amount) for r in a03] == [(r.interval_start, r.price_amount) for r in a01]


# --- acknowledgements ------------------------------------------------------------------------------

# Verbatim from ENTSO-E's knowledge base article "Rejection of Request" (15854332833300).
ACK_NO_DATA = b"""<?xml version="1.0" encoding="UTF-8"?>
<Acknowledgement_MarketDocument
\txmlns="urn:iec62325.351:tc57wg16:451-1:acknowledgementdocument:7:0">
\t<mRID>be3917c7-0704-4</mRID>
\t<createdDateTime>2016-03-30T10:45:36Z</createdDateTime>
\t<sender_MarketParticipant.mRID codingScheme="A01">10X1001A1001A450</sender_MarketParticipant.mRID>
\t<sender_MarketParticipant.marketRole.type>A32</sender_MarketParticipant.marketRole.type>
\t<receiver_MarketParticipant.mRID codingScheme="A01">10X1001A1001A450</receiver_MarketParticipant.mRID>
\t<receiver_MarketParticipant.marketRole.type>A39</receiver_MarketParticipant.marketRole.type>
\t<received_MarketDocument.createdDateTime>2016-03-30T10:45:35Z</received_MarketDocument.createdDateTime>
\t<Reason>
\t\t<code>999</code>
\t\t<text>No matching data found</text>
\t</Reason>
</Acknowledgement_MarketDocument>"""


def test_acknowledgement_no_matching_data() -> None:
    with pytest.raises(AcknowledgementError) as exc:
        decode(ACK_NO_DATA)
    assert exc.value.no_matching_data
    assert [(r.code, r.text) for r in exc.value.reasons] == [("999", "No matching data found")]
    assert not isinstance(exc.value, ParseError)


def test_acknowledgement_rejected_query_is_not_no_data() -> None:
    body = ACK_NO_DATA.replace(b"No matching data found", b"The amount of requested data exceeds allowed limit")
    with pytest.raises(AcknowledgementError) as exc:
        decode(body)
    assert not exc.value.no_matching_data


# --- malformed input -------------------------------------------------------------------------------

_DAY = ("2025-10-01T22:00Z", "2025-10-01T23:00Z")


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"not xml", id="malformed"),
        pytest.param(b"PK\x03\x04zipped", id="zip"),
        pytest.param(b'<!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>', id="dtd"),
        pytest.param(doc(root="Balancing_MarketDocument"), id="unsupported-root"),
        pytest.param(doc(series([period(*_DAY, "PT15M", [(1, 1.0)])], curve="A02")), id="curve-a02"),
        pytest.param(doc(series([period(*_DAY, "PT15M", [(1, 1.0), (1, 2.0)])])), id="duplicate-position"),
        pytest.param(doc(series([period(*_DAY, "PT15M", [(5, 1.0)])])), id="position-out-of-range"),
        pytest.param(doc(series([period(*_DAY, "PT15M", [(0, 1.0)])])), id="position-zero"),
        pytest.param(doc(series([period(*_DAY, "PT7M", [(1, 1.0)])])), id="span-not-multiple"),
        pytest.param(doc(series([period(*_DAY, "P1M", [(1, 1.0)])])), id="calendar-resolution"),
        pytest.param(doc(series([period("2025-10-01T22:00", "2025-10-01T23:00", "PT15M", [(1, 1.0)])])), id="no-utc"),
        pytest.param(doc(series([period("2025-10-02T00:00+02:00", "2025-10-01T23:00Z", "PT15M", [])])), id="offset"),
    ],
)
def test_malformed_input_is_a_parse_error(body: bytes) -> None:
    with pytest.raises(ParseError):
        decode(body)


def test_empty_document_and_period_give_no_records() -> None:
    assert decode(doc()) == []
    assert decode(doc(series([period(*_DAY, "PT15M", [])]))) == []


def test_non_numeric_and_unknown_point_elements() -> None:
    bad_number = doc(series([period(*_DAY, "PT15M", [(1, 1.0)])])).replace(b">1.0<", b">12,5<")
    with pytest.raises(ParseError, match="not a number"):
        decode(bad_number)
    unknown = doc(series([period(*_DAY, "PT15M", [(1, 1.0)])])).replace(
        b"</price.amount>", b"</price.amount><imbalance_Price.category>A04</imbalance_Price.category>"
    )
    with pytest.raises(ParseError, match="unexpected Point element"):
        decode(unknown)
