"""Offline tests for scripts/build_geometries.py (DAGI transform path).

All geometry here is SYNTHETIC: small squares in EPSG:25832 near Copenhagen, not real boundaries.
The HTTP layer is an httpx.MockTransport; no network and no real API key.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
import yaml
from pydantic import SecretStr
from shapely.geometry import Polygon

FAKE_KEY = "SYNTHETIC-KEY-0123456789abcdef"


@pytest.fixture(scope="module")
def bg(root: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_geometries", root / "scripts" / "build_geometries.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_geometries"] = mod
    spec.loader.exec_module(mod)
    return mod


def square_wkt(i: int, size: float = 2000.0, noise: bool = False) -> str:
    """Synthetic square in EPSG:25832 metres, tiled eastwards; optional extra vertex on one edge."""
    x0, y0 = 700_000.0 + i * 3000.0, 6_170_000.0
    pts = [(x0, y0), (x0 + size, y0), (x0 + size, y0 + size), (x0, y0 + size)]
    if noise:
        pts.insert(1, (x0 + size / 2, y0 + 0.5))  # near-collinear: simplification must drop it
    return Polygon(pts).wkt


def node(code: str, wkt_text: str, crs: str = "EPSG:25832") -> dict[str, Any]:
    return {"id_lokalId": f"id-{code}", "kommunekode": code, "geometri": {"crs": crs, "wkt": wkt_text}}


def page(nodes: list[dict[str, Any]], end: str | None = None) -> bytes:
    conn = {"nodes": nodes, "pageInfo": {"hasNextPage": end is not None, "endCursor": end}}
    return json.dumps({"data": {"DAGI_Kommuneinddeling": conn}}).encode()


def fake_names(n: int = 98) -> dict[str, str]:
    return {str(101 + i): f"Kommune {i}" for i in range(n)}


def test_reprojection_lands_in_denmark(bg: ModuleType) -> None:
    g = bg.reproject(Polygon([(720_000, 6_176_000), (721_000, 6_176_000), (721_000, 6_177_000)]), 25832)
    lon, lat = g.exterior.coords[0]
    assert 12.3 < lon < 12.8 and 55.5 < lat < 55.9  # about Copenhagen
    assert bg.epsg_of("urn:ogc:def:crs:EPSG::25832") == 25832
    assert bg.epsg_of("EPSG:25832") == 25832
    assert bg.epsg_of(25832) == 25832  # what the live service actually returns
    with pytest.raises(bg.DagiError):
        bg.epsg_of("bogus")


def test_simplification_drops_redundant_vertices(bg: ModuleType) -> None:
    g = bg.reproject(shapely_poly(bg), 25832)
    out = bg.geojson_geometry(g, 0.0001, bg.DAGI_PRECISION)
    assert out["type"] == "Polygon"
    assert len(out["coordinates"][0]) == 5  # square, closed ring
    assert all(len(str(c).split(".")[-1]) <= bg.DAGI_PRECISION for pt in out["coordinates"][0] for c in pt)


def shapely_poly(bg: ModuleType) -> Polygon:
    from shapely import wkt

    return wkt.loads(square_wkt(0, noise=True))


def test_kommunekode_mapping_and_integer_form(bg: ModuleType) -> None:
    names = fake_names()
    nodes = [node(f"{int(c):04d}", square_wkt(i)) for i, c in enumerate(names)]
    pages = [page(nodes[:50], end="c1"), page(nodes[50:])]
    rows = bg.dagi_rows(pages, names)
    assert len(rows) == 98
    assert rows[0][:2] == ("101", "Kommune 0")


def test_missing_or_extra_code_fails_loudly(bg: ModuleType) -> None:
    names = fake_names()
    nodes = [node(f"{int(c):04d}", square_wkt(i)) for i, c in enumerate(names)]
    with pytest.raises(bg.DagiError, match="missing"):
        bg.dagi_rows([page(nodes[:-1])], names)
    with pytest.raises(bg.DagiError, match="unexpected"):
        bg.dagi_rows([page([*nodes, node("0999", square_wkt(99))])], names)
    with pytest.raises(bg.DagiError, match="twice"):
        bg.dagi_rows([page([*nodes, nodes[0]])], names)
    # Christiansoe (0411) is ignored, not an error
    assert len(bg.dagi_rows([page([*nodes, node("0411", square_wkt(98))])], names)) == 98


def test_redact(bg: ModuleType) -> None:
    text = f"GET https://x.example/v2?apiKey={FAKE_KEY}&a=1 failed; also {FAKE_KEY} and apikey=other"
    out = bg.redact(text, FAKE_KEY)
    assert FAKE_KEY not in out and "other" not in out
    assert "a=1" in out


def mock_client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_pages_and_key_sent_as_param(bg: ModuleType) -> None:
    seen: list[httpx.Request] = []
    bodies = [page([node("0101", square_wkt(0))], end="c1"), page([node("0147", square_wkt(1))])]

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, content=bodies[len(seen) - 1])

    pages = bg.fetch_dagi(mock_client(handler), bg.DAGI_URL, FAKE_KEY)
    assert pages == bodies
    assert all(r.url.params["apiKey"] == FAKE_KEY for r in seen)
    assert 'after: "c1"' in json.loads(seen[1].content)["query"]


def test_http_error_message_never_contains_key(bg: ModuleType) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"bad key {FAKE_KEY} for {req.url}")

    with pytest.raises(bg.DagiError) as exc:
        bg.fetch_dagi(mock_client(handler), bg.DAGI_URL, FAKE_KEY)
    assert FAKE_KEY not in str(exc.value) and "401" in str(exc.value)

    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {req.url}")

    with pytest.raises(bg.DagiError) as exc2:
        bg.fetch_dagi(mock_client(boom), bg.DAGI_URL, FAKE_KEY)
    assert FAKE_KEY not in str(exc2.value)


def test_graphql_error_lists_fields(bg: ModuleType) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if "__type" in json.loads(req.content)["query"]:
            data = {"data": {"__type": {"fields": [{"name": "kommunekode"}, {"name": "geometri2"}]}}}
            return httpx.Response(200, json=data)
        return httpx.Response(200, json={"errors": [{"message": f"no field geometri ({req.url})"}]})

    with pytest.raises(bg.DagiError) as exc:
        bg.fetch_dagi(mock_client(handler), bg.DAGI_URL, FAKE_KEY)
    msg = str(exc.value)
    assert "geometri2" in msg and FAKE_KEY not in msg


def test_end_to_end_outputs_and_no_key_in_files(
    bg: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = fake_names()
    ents = [
        {"id": f"dk.municipality.{bg.slug(n)}", "kind": "municipality", "name": n, "attributes": {"kommunekode": c}}
        for c, n in names.items()
    ]
    ep = tmp_path / "entities.yaml"
    ep.write_text(yaml.safe_dump({"entities": ents}), encoding="utf-8")
    nodes = [node(f"{int(c):04d}", square_wkt(i, noise=True)) for i, c in enumerate(names)]

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=page(nodes))

    class Settings:
        datafordeler_api_key = SecretStr(FAKE_KEY)
        user_agent = "test"

    real_client = httpx.Client
    monkeypatch.setattr(bg, "OUT", tmp_path)
    monkeypatch.setattr(bg, "ENTITIES", ep)
    monkeypatch.setattr(bg, "get_settings", lambda: Settings())
    monkeypatch.setattr(bg.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    assert bg.main(["--source", "dagi"]) == 0

    feats = json.loads((tmp_path / "municipalities.geojson").read_text(encoding="utf-8"))["features"]
    assert len(feats) == 98
    assert feats[0]["properties"] == {
        "entity_id": "dk.municipality.kommune_0", "kommunekode": "101", "name": "Kommune 0",
    }  # fmt: skip
    assert len(json.loads((tmp_path / "zones.geojson").read_text(encoding="utf-8"))["features"]) == 2
    for f in tmp_path.iterdir():
        assert FAKE_KEY not in f.read_text(encoding="utf-8")
    meta = yaml.safe_load((tmp_path / "municipalities.meta.yaml").read_text(encoding="utf-8"))
    assert meta["source_url"] == bg.DAGI_URL and "apikey" not in meta["source_url"].lower()
    assert "EPSG:25832" in meta["transformation"]
