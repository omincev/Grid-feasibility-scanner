"""Snapshot runtime (WP1.4): POST entries, the Power BI resource key from the embed page, change-hash
normalisers, and the connection-queue parser on the recorded querydata response."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from scanner.adapters.energinet import connection_queue as cq
from scanner.core.registry import Snapshot, load_snapshots
from scanner.runtime import snapshots

QUEUE_FIXTURE = Path(__file__).parent / "fixtures" / "energinet" / "connection_queue__capacity_by_region_phase.json"
FAKE_KEY = "00000000-0000-0000-0000-000000000000"


def queue_response() -> Any:
    return json.loads(QUEUE_FIXTURE.read_bytes())


def with_updated_column(response: Any, text: str = "Senest opdateret: 01-10-2026") -> Any:
    """The recorded response with its second measure renamed to the last-updated measure the registry
    query asks for (same DSR shape: one dictionary value repeated on every row)."""
    r = copy.deepcopy(response)
    data = r["results"][0]["result"]["data"]
    data["descriptor"]["Select"][4]["Name"] = cq.UPDATED
    data["dsr"]["DS"][0]["ValueDicts"]["D3"] = [text]
    return r


def embed_page(key: str = FAKE_KEY) -> str:
    token = base64.b64encode(json.dumps({"k": key, "t": "tenant", "c": 8}).encode()).decode()
    return f'<html><script>var x = "\\u0022https://app.powerbi.com/view?r={token}\\u0026amp;pageName=88a3";</script>'


def snapshot(**overrides: Any) -> Snapshot:
    base: dict[str, Any] = {
        "id": "t",
        "url": "https://example.org/x",
        "kind": "web_page",
        "render": "http",
        "schedule": "0 6 * * 1",
        "timezone": "Europe/Copenhagen",
        "on_change": "none",
        "owner": "t",
        "status": "active",
    }
    return Snapshot(**(base | overrides))


def test_registry_queue_entry_is_a_post_with_its_query(root: Path) -> None:
    snaps = {s.id: s for s in load_snapshots(root)}
    q = snaps["dk.energinet.connection_queue"]
    assert (q.method, q.kind, q.render, q.on_change) == ("POST", "json", "http", "parse:connection_queue")
    assert q.powerbi_embed_page and "X-PowerBI-ResourceKey" not in q.headers
    assert q.body is not None and q.body["modelId"] == 4546001
    query = q.body["queries"][0]["Query"]["Commands"][0]["SemanticQueryDataShapeCommand"]["Query"]
    assert {f["Entity"] for f in query["From"]} == {"Dim Placering", "Dim Type", "Dim Fase", "Målinger"}
    assert [s["Name"] for s in query["Select"]] == [cq.REGION, cq.TYPE, cq.PHASE, cq.CAPACITY, cq.UPDATED]
    assert snaps["dk.energinet.connection_queue.page"].kind == "web_page"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"body": {"a": 1}}, "need method POST"),
        ({"powerbi_embed_page": "https://example.org"}, "need method POST"),
        ({"render": "browser", "method": "POST"}, "plain page loads"),
        ({"ignore_keys": ["timestamp"]}, "json snapshots only"),
        ({"kind": "json", "normalise": "visible_text"}, "HTML snapshots only"),
    ],
)
def test_snapshot_model_rejects(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        snapshot(**overrides)


def test_resource_key_comes_from_the_embed_url() -> None:
    assert cq.powerbi_resource_key(embed_page()) == FAKE_KEY
    with pytest.raises(cq.ParseError, match=r"no app\.powerbi\.com"):
        cq.powerbi_resource_key("<html>no report here</html>")


def test_post_sends_body_headers_and_the_key_read_from_the_page(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, text=embed_page())
        return httpx.Response(200, content=QUEUE_FIXTURE.read_bytes(), headers={"content-type": "text/plain"})

    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    snap = snapshot(
        kind="json",
        method="POST",
        url="https://wabi.example/querydata?synchronous=true",
        headers={"Content-Type": "application/json"},
        body={"modelId": 1, "q": "Målinger"},
        powerbi_embed_page="https://example.org/page",
    )
    status, _, content = snapshots._fetch_http(snap)
    assert status == 200 and content == QUEUE_FIXTURE.read_bytes()
    page, post = seen
    assert (page.method, str(page.url)) == ("GET", "https://example.org/page")
    assert post.method == "POST" and post.headers["X-PowerBI-ResourceKey"] == FAKE_KEY
    assert post.headers["Content-Type"] == "application/json" and "grid-scanner" in post.headers["User-Agent"]
    assert json.loads(post.content) == {"modelId": 1, "q": "Målinger"}


def test_visible_text_ignores_per_request_noise_but_not_content() -> None:
    page = (
        '<html><head><style>p{{}}</style><script>var t="{t}";</script></head><body>'
        '<li id="{i}"><a href="/doc.pdf#p{i}">Kapacitetskort</a></li><!-- {t} -->'
        '<a href="/cdn-cgi/l/email-protection" data-cfemail="{t}">[email&#160;protected]</a>'
        '<iframe src="https://app.powerbi.com/view?r=abc"></iframe><p>Senest opdateret {d}</p></body></html>'
    )
    snap = snapshot(normalise="visible_text")
    a = snapshots.normalised(snap, page.format(t="9df6", i="x1", d="01-10-2026").encode())
    b = snapshots.normalised(snap, page.format(t="6d06", i="q7", d="01-10-2026").encode())
    c = snapshots.normalised(snap, page.format(t="6d06", i="q7", d="01-11-2026").encode())
    assert a == b != c
    assert b"Kapacitetskort" in a and b"/doc.pdf" in a and b"app.powerbi.com/view?r=abc" in a
    assert b"var t" not in a and b"p{}" not in a
    plain = snapshot()  # pages that render from scripts keep the raw bytes
    assert snapshots.normalised(plain, b"<script>x</script>") == b"<script>x</script>"


def test_ignore_keys_drop_per_request_ids_from_the_hash() -> None:
    snap = snapshot(kind="json", ignore_keys=["jobIds", "jobId", "timestamp", "rootActivityId"])
    first = queue_response()
    second = copy.deepcopy(first)
    second["jobIds"] = ["other"]
    second["results"][0]["jobId"] = "other"
    second["results"][0]["result"]["data"]["timestamp"] = "2026-11-02T06:00:00Z"
    second["results"][0]["result"]["data"]["rootActivityId"] = "other"
    h1, h2 = (hashlib.sha256(snapshots.normalised(snap, json.dumps(r).encode())).hexdigest() for r in (first, second))
    assert h1 == h2
    third = copy.deepcopy(second)
    third["results"][0]["result"]["data"]["dsr"]["DS"][0]["PH"][0]["DM0"][4]["C"][2] = 1965
    changed, original = (snapshots.normalised(snap, json.dumps(r).encode()) for r in (third, first))
    assert changed != original
    assert snapshots.normalised(snap, b"<html>error</html>") == b"<html>error</html>"


def test_decode_dsr_repeat_and_null_bitmasks() -> None:
    columns, rows = cq.decode_dsr(queue_response())
    assert columns == [cq.REGION, cq.TYPE, cq.PHASE, cq.CAPACITY, "m.Antal projekter i alt med failsafe"]
    assert rows[0] == ["Region Hovedstaden", None, "Screening", None, "'Ikke oplyst'"]  # padding row
    assert rows[4] == ["Region Hovedstaden", "Forbrug", "Screening", 1964, "'Ikke oplyst'"]
    assert rows[5] == ["Region Hovedstaden", "Forbrug", "Modning", 390, "'Ikke oplyst'"]  # R copies region, type
    assert rows[7] == ["Region Hovedstaden", "Forbrug", "", None, "'Ikke oplyst'"]  # Ø nulls the measure


def test_queue_cells_match_the_published_totals() -> None:
    cells, valid_from = cq.queue_cells(queue_response())
    assert len(cells) == 30 and valid_from is None  # the recorded query did not ask for the date
    assert {c.region for c in cells} == set(cq.REGION_ZONES)

    def total(direction: str) -> float:
        return round(sum(c.mw or 0 for c in cells if f".{direction}_" in c.entry_id), 2)

    assert total("consumption") == 36494.54 and total("production") == 24287.69  # the page's totals
    syd = next(c for c in cells if c.region == "Region Syddanmark" and c.entry_id.endswith("production_establishment"))
    assert syd.mw == 2201.2999999999997  # published as the string "2201.2999999999997"


def test_queue_valid_from_is_the_last_updated_date() -> None:
    _, valid_from = cq.queue_cells(with_updated_column(queue_response()))
    assert valid_from == datetime(2026, 10, 1, tzinfo=UTC)
    with pytest.raises(cq.ParseError, match="DD-MM-YYYY"):
        cq.queue_cells(with_updated_column(queue_response(), "Senest opdateret: oktober"))


def test_queue_rejects_an_unknown_phase() -> None:
    r = queue_response()
    r["results"][0]["result"]["data"]["dsr"]["DS"][0]["ValueDicts"]["D2"][0] = "Ny fase"
    with pytest.raises(cq.ParseError, match="unexpected queue cell"):
        cq.queue_cells(r)


def test_region_entities() -> None:
    assert cq.region_entity("Region Sjælland") == ("dk.tso_area.region_sjaelland", "dk.zone.dk2")
    assert cq.region_entity("Region Nordjylland") == ("dk.tso_area.region_nordjylland", "dk.zone.dk1")
    assert cq.region_entity("Region Ukendt") == ("dk.tso_area.region_ukendt", "dk.country.dk")
