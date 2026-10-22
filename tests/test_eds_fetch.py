"""Energi Data Service request building and failure handling, offline (httpx.MockTransport)."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from scanner.adapters.energidataservice import EnergiDataServiceAdapter
from scanner.adapters.energidataservice import adapter as adapter_module
from scanner.adapters.energidataservice.adapter import ATTEMPTS
from scanner.core.time import local_day_bounds
from scanner.runtime.context import RepoState

START = datetime(2025, 10, 25, 22, tzinfo=UTC)
END = datetime(2025, 10, 26, 23, tzinfo=UTC)
EMPTY = b'{"total":0,"filters":"","sort":"TimeUTC asc","limit":0,"dataset":"DayAheadPrices","records":[]}'


def _adapter(handler: Callable[[httpx.Request], httpx.Response]) -> tuple[EnergiDataServiceAdapter, list[float]]:
    sleeps: list[float] = []
    adapter = EnergiDataServiceAdapter(client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=sleeps.append)
    return adapter, sleeps


def test_request_uses_utc_window_and_returns_everything(state: RepoState) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=EMPTY, headers={"content-type": "application/json; charset=utf-8"})

    m = next(m for m in state.manifests if m.dataset == "DayAheadPrices")
    adapter, sleeps = _adapter(handler)
    (result,) = adapter.fetch(m, START, END)
    params = seen[0].url.params
    assert seen[0].url.path == "/dataset/DayAheadPrices"
    assert params["start"] == "2025-10-25T22:00" and params["end"] == "2025-10-26T23:00"
    assert params["timezone"] == "UTC"  # otherwise start/end are read as Danish local time
    assert params["limit"] == "0"  # all rows; the default is 100
    assert params["sort"] == "TimeUTC asc"
    assert json.loads(params["filter"]) == {"PriceArea": sorted(m.zones)}
    assert result.status == 200 and result.ext == "json" and sleeps == []


def test_429_waits_for_retry_after(state: RepoState) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "9"}, content=b"Rate limit is exceeded.")
        return httpx.Response(200, content=EMPTY, headers={"content-type": "application/json"})

    m = next(m for m in state.manifests if m.dataset == "ImbalancePrice")
    adapter, sleeps = _adapter(handler)
    (result,) = adapter.fetch(m, START, END)
    assert result.status == 200
    assert sleeps == [10.0]


@pytest.mark.parametrize("status", [429, 503])
def test_final_error_status_is_returned_for_the_runtime_to_store(state: RepoState, status: int) -> None:
    m = next(m for m in state.manifests if m.dataset == "ImbalancePrice")
    adapter, sleeps = _adapter(lambda r: httpx.Response(status, content=b'{"statusCode": %d}' % status))
    (result,) = adapter.fetch(m, START, END)
    assert result.status == status
    assert result.content
    assert len(sleeps) == ATTEMPTS - 1


def test_transport_error_is_raised_after_retries(state: RepoState) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("connection refused", request=request)

    m = next(m for m in state.manifests if m.dataset == "ImbalancePrice")
    adapter, sleeps = _adapter(handler)
    with pytest.raises(httpx.ConnectError):
        adapter.fetch(m, START, END)
    assert len(calls) == ATTEMPTS and len(sleeps) == ATTEMPTS - 1


def test_retired_dataset_backfill_reuses_one_response(state: RepoState, monkeypatch: pytest.MonkeyPatch) -> None:
    """Elspotprices allows one call per ~5 minutes: consecutive daily partitions share a 31-day request."""
    monkeypatch.setattr(adapter_module, "_retired_responses", {})
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=EMPTY, headers={"content-type": "application/json"})

    m = next(m for m in state.manifests if m.dataset == "Elspotprices")
    assert m.status == "backfill_only"
    adapter, _ = _adapter(handler)
    day1 = local_day_bounds(date(2025, 6, 1), "Europe/Copenhagen")
    day2 = local_day_bounds(date(2025, 6, 2), "Europe/Copenhagen")
    (first,) = adapter.fetch(m, *day1)
    (second,) = adapter.fetch(m, *day2)
    assert second is first and len(seen) == 1
    assert seen[0].url.params["end"] == (day1[0] + timedelta(days=31)).strftime("%Y-%m-%dT%H:%M")
    adapter.fetch(m, *local_day_bounds(date(2025, 7, 5), "Europe/Copenhagen"))  # outside the span
    assert len(seen) == 2


def test_client_error_is_not_retried(state: RepoState) -> None:
    m = next(m for m in state.manifests if m.dataset == "ImbalancePrice")
    adapter, sleeps = _adapter(lambda r: httpx.Response(400, content=b"Invalid column Nope"))
    (result,) = adapter.fetch(m, START, END)
    assert result.status == 400 and sleeps == []


def test_active_dataset_history_shares_one_response_but_recent_days_do_not(
    state: RepoState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With EDS_HISTORY_SPAN_DAYS set, days older than the lookback share one request; recent days do not."""
    monkeypatch.setenv("EDS_HISTORY_SPAN_DAYS", "366")
    monkeypatch.setattr(adapter_module, "_retired_responses", {})
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=EMPTY, headers={"content-type": "application/json"})

    m = next(m for m in state.manifests if m.dataset == "ImbalancePrice")
    assert m.status == "active"
    adapter, _ = _adapter(handler)
    (first,) = adapter.fetch(m, *local_day_bounds(date(2025, 6, 1), "Europe/Copenhagen"))
    (second,) = adapter.fetch(m, *local_day_bounds(date(2025, 9, 1), "Europe/Copenhagen"))
    assert second is first and len(seen) == 1
    today = datetime.now(UTC).date()
    adapter.fetch(m, *local_day_bounds(today, "Europe/Copenhagen"))
    adapter.fetch(m, *local_day_bounds(today, "Europe/Copenhagen"))
    assert len(seen) == 3  # each recent window is requested on its own
