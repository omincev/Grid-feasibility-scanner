"""EntsoeAdapter (src/scanner/adapters/entsoe/adapter.py) offline, on recorded fixtures.

The fixtures are real responses recorded by scripts/record_entsoe_fixtures.py. FAKE_TOKEN is SYNTHETIC;
every request goes to an httpx.MockTransport.
"""

from __future__ import annotations

import itertools
import json
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from conftest import FIXTURES
from pydantic import SecretStr

from scanner.adapters.base import FetchResult, ParsedBatch, ParseError
from scanner.adapters.entsoe import EntsoeAdapter
from scanner.adapters.entsoe.adapter import QUERIES, _requested_window, chunks
from scanner.core.manifest import Manifest
from scanner.core.time import local_day, local_day_bounds, parse_source_utc
from scanner.runtime.context import RepoState

FAKE_TOKEN = "22222222-synthetic-adapter-token-2222"
DK1, DE_LU, NL = "10YDK-1--------W", "10Y1001A1001A82H", "10YNL----------L"


def manifest(state: RepoState, dataset: str) -> Manifest:
    return next(m for m in state.manifests if m.source == "entsoe" and m.dataset == dataset)


def adapter(handler: Callable[[httpx.Request], httpx.Response] | None = None) -> EntsoeAdapter:
    handler = handler or (lambda r: httpx.Response(599))
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return EntsoeAdapter(client, token=SecretStr(FAKE_TOKEN), sleep=lambda s: None)


def result(name: str, start: str, end: str, status: int = 200) -> FetchResult:
    """A recorded response as the adapter's fetch returns it (periodStart/periodEnd as requested)."""
    meta = json.loads((FIXTURES / "entsoe" / f"{name}.xml.meta.json").read_text(encoding="utf-8"))
    return FetchResult(
        url=meta["url"],  # recorded fixtures; the official examples have a documentation URL
        params={**meta.get("params", {}), "periodStart": start, "periodEnd": end},
        status=status,
        content_type="text/xml",
        content=(FIXTURES / "entsoe" / f"{name}.xml").read_bytes(),
        fetched_at=datetime(2026, 10, 18, tzinfo=UTC),
    )


def recorded(name: str) -> FetchResult:
    meta = json.loads((FIXTURES / "entsoe" / f"{name}.xml.meta.json").read_text(encoding="utf-8"))
    return result(name, meta["params"]["periodStart"], meta["params"]["periodEnd"])


def parse(state: RepoState, dataset: str, r: FetchResult) -> ParsedBatch:
    m = manifest(state, dataset)
    lo = datetime(2020, 1, 1, tzinfo=UTC)
    hi = datetime(2030, 1, 1, tzinfo=UTC)
    return adapter().parse(m, state.countries["eu"], state.entries_for(m), r, lo, hi)


def by_key(batch: ParsedBatch) -> Counter[str]:
    return Counter(r.series_key for r in batch.rows)


# --- manifests -------------------------------------------------------------------------------------


def test_manifest_zones_come_from_the_registry(state: RepoState) -> None:
    prices = manifest(state, "EnergyPrices")
    assert prices.zones[DK1] == "dk.zone.dk1" and prices.zones[NL] == "nl.zone.nl"
    assert prices.zones[DE_LU] == "de.zone.de_lu"
    verified = {z.eic: z.id for c in state.countries.values() for z in c.zones if z.eic and z.eic_verified}
    assert prices.zones == verified  # scope "all"
    for dataset in ("ActualTotalLoad", "ActualGenerationPerType"):  # scope "active": dk and nl only
        assert set(manifest(state, dataset).zones.values()) == {"dk.zone.dk1", "dk.zone.dk2", "nl.zone.nl"}
    assert set(QUERIES) == {m.dataset for m in state.manifests if m.source == "entsoe"}


# --- fetch -----------------------------------------------------------------------------------------


def test_fetch_one_request_per_zone_token_in_header(state: RepoState) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"<x/>", headers={"content-type": "text/xml"})

    m = manifest(state, "ActualTotalLoad")
    # A recent window (inside REUSE_MIN_AGE), so the request is never widened; fixed dates go stale.
    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(days=1)
    start = end - timedelta(days=3)
    results = adapter(handler).fetch(m, start, end)
    assert len(results) == len(seen) == len(m.zones)
    for req, res in zip(seen, results, strict=True):
        q = dict(req.url.params)
        assert q["documentType"] == "A65" and q["processType"] == "A16"
        assert q["outBiddingZone_Domain"] in m.zones
        assert (q["periodStart"], q["periodEnd"]) == (start.strftime("%Y%m%d%H%M"), end.strftime("%Y%m%d%H%M"))
        assert req.headers["SECURITY_TOKEN"] == FAKE_TOKEN
        assert FAKE_TOKEN not in str(req.url) and FAKE_TOKEN not in json.dumps(res.params)


def test_long_windows_are_chunked_within_the_one_year_limit(state: RepoState) -> None:
    start, end = datetime(2025, 3, 3, 23, tzinfo=UTC), datetime(2026, 10, 17, 22, tzinfo=UTC)
    pieces = chunks(start, end)
    assert pieces[0][0] == start and pieces[-1][1] == end and len(pieces) == 2
    assert all(b - a <= timedelta(days=365) for a, b in pieces)
    assert all(a[1] == b[0] for a, b in itertools.pairwise(pieces))


def _daily_windows(m: Manifest, first: date, days: int) -> list[tuple[datetime, datetime]]:
    """The fetch windows of consecutive daily runs (Brussels days, manifest lookback and lookahead)."""
    out = []
    for i in range(days):
        a, b = local_day_bounds(first + timedelta(days=i), "Europe/Brussels")
        out.append((a - m.lookback_td, b + m.lookahead_td))
    return out


@pytest.mark.parametrize(
    ("dataset", "per_zone"), [("EnergyPrices", 2), ("ActualTotalLoad", 2), ("ActualGenerationPerType", 8)]
)
def test_backfill_reuses_wide_responses_across_daily_runs(state: RepoState, dataset: str, per_zone: int) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"<x/>", headers={"content-type": "text/xml"})

    m = manifest(state, dataset)
    a = adapter(handler)
    windows = _daily_windows(m, date(2025, 3, 4), 40)
    for start, end in windows:
        for res in a.fetch(m, start, end):
            lo, hi = _requested_window(res)
            assert lo <= start and end <= hi  # every run's window lies inside the response it gets
            assert FAKE_TOKEN not in json.dumps(res.params) and FAKE_TOKEN not in res.url
    assert len(seen) == per_zone * len(m.zones)
    assert all(req.headers["SECURITY_TOKEN"] == FAKE_TOKEN and FAKE_TOKEN not in str(req.url) for req in seen)


def test_recent_windows_always_hit_the_api(state: RepoState) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"<x/>", headers={"content-type": "text/xml"})

    m = manifest(state, "ActualTotalLoad")
    a = adapter(handler)
    today = local_day(datetime.now(UTC), "Europe/Brussels")
    for day in (today, today, today - timedelta(days=3), today - timedelta(days=3)):
        a.fetch(m, *local_day_bounds(day, "Europe/Brussels"))
    assert len(seen) == 4 * len(m.zones)
    # The window is not widened either: the response answers exactly what was asked.
    assert all(
        req.url.params["periodEnd"] <= (datetime.now(UTC) + timedelta(days=2)).strftime("%Y%m%d%H%M") for req in seen
    )


def test_failed_response_is_not_reused(state: RepoState) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(400, content=b"<x/>")

    m = manifest(state, "ActualTotalLoad")
    a = adapter(handler)
    for start, end in _daily_windows(m, date(2025, 3, 4), 2):
        a.fetch(m, start, end)
    assert len(seen) == 2 * len(m.zones)


def test_missing_token_fails_before_any_request(state: RepoState, monkeypatch: pytest.MonkeyPatch) -> None:
    from scanner.adapters.entsoe import adapter as module

    monkeypatch.setattr(module, "get_settings", lambda: type("S", (), {"entsoe_api_token": None})())
    a = EntsoeAdapter(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(599))))
    with pytest.raises(RuntimeError, match="ENTSOE_API_TOKEN"):
        a.fetch(manifest(state, "EnergyPrices"), datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC))


# --- parse: day-ahead prices -----------------------------------------------------------------------


def test_resolution_switch_gives_two_series(state: RepoState) -> None:
    batch = parse(state, "EnergyPrices", recorded("day_ahead_prices__dk1__2025-09-30_2025-10-01_resolution_switch"))
    hourly = "eu.entsoe.EnergyPrices.dk.zone.dk1.price_amount.pt1h"
    quarter = "eu.entsoe.EnergyPrices.dk.zone.dk1.price_amount.pt15m"
    assert by_key(batch) == {hourly: 24, quarter: 96}
    assert batch.series[hourly].resolution == timedelta(hours=1)
    assert batch.series[quarter].resolution == timedelta(minutes=15)
    last_hour = max(r.interval_end for r in batch.rows if r.series_key == hourly)
    first_quarter = min(r.interval_start for r in batch.rows if r.series_key == quarter)
    assert last_hour == first_quarter == datetime(2025, 9, 30, 22, tzinfo=UTC)  # disjoint, adjacent
    assert all(r.issued_at is None and r.quality == "ok" for r in batch.rows)


@pytest.mark.parametrize(
    ("case", "n"),
    [("day_ahead_prices__dk1__2025-10-26_dst_end", 100), ("day_ahead_prices__dk1__2026-03-29_dst_start", 92)],
)
def test_dst_days(state: RepoState, case: str, n: int) -> None:
    # The DST-start response also carries a sequence-2 copy of the prices: it must not double the rows.
    batch = parse(state, "EnergyPrices", recorded(case))
    assert by_key(batch) == {"eu.entsoe.EnergyPrices.dk.zone.dk1.price_amount.pt15m": n}


def test_only_the_sdac_sequence_is_kept_and_matches_energi_data_service(state: RepoState) -> None:
    batch = parse(state, "EnergyPrices", recorded("day_ahead_prices__de_lu__2026-10-14"))
    assert by_key(batch) == {"eu.entsoe.EnergyPrices.de.zone.de_lu.price_amount.pt15m": 96}
    eds = json.loads((FIXTURES / "energidataservice" / "DayAheadPrices__normal_day.json").read_bytes())["records"]
    de = {parse_source_utc(r["TimeUTC"]): r["DayAheadPriceEUR"] for r in eds if r["PriceArea"] == "DE"}
    for row in batch.rows:
        assert row.value == pytest.approx(de[row.interval_start], abs=1e-4)


def test_a03_and_a01_responses_give_the_same_rows(state: RepoState) -> None:
    a03 = parse(state, "EnergyPrices", recorded("day_ahead_prices__dk1__2026-10-14"))
    a01 = parse(state, "EnergyPrices", recorded("day_ahead_prices__dk1__2026-10-14__curve_a01"))
    assert len(a03.rows) == 96 and a03.rows == a01.rows


NO2 = "10YNO-2--------T"
NO2_DUPLICATE = "day_ahead_prices__no2__2026-08-08__duplicate_timeseries"


def test_identical_repeated_time_series_collapse_to_one(state: RepoState) -> None:
    """Real NO2 response (2026-08-08): two TimeSeries, mRID 1 and 2, same headers and same 96 values."""
    batch = parse(state, "EnergyPrices", recorded(NO2_DUPLICATE))
    assert by_key(batch) == {"eu.entsoe.EnergyPrices.no.zone.no2.price_amount.pt15m": 96}
    assert len({row.interval_start for row in batch.rows}) == 96


def test_repeated_time_series_with_different_values_raise_naming_both(state: RepoState) -> None:
    r = recorded(NO2_DUPLICATE)
    content = r.content.decode("utf-8")
    first, sep, rest = content.partition("<mRID>2</mRID>")
    altered = first + sep + rest.replace("<price.amount>148.38</price.amount>", "<price.amount>149.5</price.amount>", 1)
    assert altered != content
    with pytest.raises(ParseError, match=r"no\.zone\.no2.*2026-08-07T22:00:00\+00:00: 148\.38 and 149\.5"):
        parse(state, "EnergyPrices", r.__class__(**{**r.__dict__, "content": altered.encode("utf-8")}))


def test_nl_resolves_from_its_country_file(state: RepoState) -> None:
    batch = parse(state, "EnergyPrices", recorded("day_ahead_prices__nl__2026-10-14"))
    (info,) = batch.series.values()
    assert info.entity_id == "nl.zone.nl" and info.entry.equivalence_class == "day_ahead_price"
    assert len(batch.rows) == 96


def test_parse_keeps_only_the_requested_chunk(state: RepoState) -> None:
    """Daily articles return whole days; neighbouring chunks must not yield the same interval twice."""
    r = result("day_ahead_prices__dk1__2025-09-30_2025-10-01_resolution_switch", "202509300000", "202509302200")
    batch = parse(state, "EnergyPrices", r)
    assert min(row.interval_start for row in batch.rows) == datetime(2025, 9, 30, 0, tzinfo=UTC)
    assert max(row.interval_end for row in batch.rows) == datetime(2025, 9, 30, 22, tzinfo=UTC)


def test_unit_mismatch_is_a_parse_error(state: RepoState) -> None:
    r = recorded("day_ahead_prices__dk1__2026-10-14")
    bad = FetchResult(**{**r.__dict__, "content": r.content.replace(b">EUR<", b">DKK<")})
    with pytest.raises(ParseError, match="source unit"):
        parse(state, "EnergyPrices", bad)


# --- parse: load and generation --------------------------------------------------------------------


def test_load_keeps_source_gaps(state: RepoState) -> None:
    batch = parse(state, "ActualTotalLoad", recorded("actual_total_load__dk1__2026-10-14"))
    assert by_key(batch) == {"eu.entsoe.ActualTotalLoad.dk.zone.dk1.quantity.pt1h": 15}  # 9 hours unpublished


def test_generation_keeps_three_types_of_generation_series(state: RepoState) -> None:
    batch = parse(state, "ActualGenerationPerType", recorded("actual_generation_per_type__dk1__2026-10-14"))
    prefix = "eu.entsoe.ActualGenerationPerType.dk.zone.dk1.quantity"
    assert by_key(batch) == {f"{prefix}.{t}.pt15m": 96 for t in ("solar", "wind_onshore", "wind_offshore")}
    classes = {i.entry.equivalence_class for i in batch.series.values()}
    assert classes == {f"generation_actual_power.{t}" for t in ("solar", "wind_onshore", "wind_offshore")}


def test_official_example_consumption_series_are_not_generation(state: RepoState) -> None:
    """The official DE example has out-zone (consumption) series for B16 and B19 too."""
    m = manifest(state, "ActualGenerationPerType").model_copy(update={"zones": {"10Y1001A1001A83F": "de.zone.de_lu"}})
    r = result("postman__A75_DE_2023-08-16", "202308152200", "202308162200")
    lo, hi = datetime(2020, 1, 1, tzinfo=UTC), datetime(2030, 1, 1, tzinfo=UTC)
    batch = adapter().parse(m, state.countries["eu"], state.entries_for(m), r, lo, hi)
    assert set(by_key(batch).values()) == {96} and len(batch.series) == 3


# --- acknowledgements ------------------------------------------------------------------------------


def test_no_data_acknowledgement_is_empty(state: RepoState) -> None:
    r = recorded("no_data__day_ahead_prices__dk1__2027-01-15")
    assert adapter().records(r) == []
    assert parse(state, "EnergyPrices", r).rows == []


def test_other_acknowledgements_are_parse_errors(state: RepoState) -> None:
    r = recorded("no_data__day_ahead_prices__dk1__2027-01-15")
    rejected = FetchResult(**{**r.__dict__, "content": r.content.replace(b"No matching data found", b"Invalid query")})
    with pytest.raises(ParseError, match="Invalid query"):
        adapter().records(rejected)


def test_records_have_the_fields_the_runtime_needs(state: RepoState) -> None:
    m = manifest(state, "EnergyPrices")
    recs: list[dict[str, Any]] = adapter().records(recorded("day_ahead_prices__nl__2026-10-14"))
    assert all(m.time_field_utc in r and m.zone_field in r for r in recs)
    assert parse_source_utc(recs[0][m.time_field_utc]) == datetime(2026, 10, 13, 22, tzinfo=UTC)
