"""Build Danish municipality and bidding-zone geometries (registry/geometries/dk).

Sources:
  dagi   Klimadatastyrelsen DAGI (entity DAGI_Kommuneinddeling) through the Datafordeler GraphQL
         service, full resolution, EPSG:25832 reprojected to EPSG:4326. Needs DATAFORDELER_API_KEY,
         read only via scanner.settings and never printed, logged or written. Default when the key is set.
  gisco  Eurostat GISCO LAU 2023 (1:1M, EPSG:4326). Token-free fallback; LAU_ID equals the kommunekode.
         DAWA is shut down (HTTP 410) and the old Dataforsyningen DAGI needs a token.
Re-runnable; prints the SHA-256 of the download and writes it to the meta files. See docs/sources/dk/dagi.md.

  uv run python scripts/build_geometries.py --source dagi
  uv run python scripts/build_geometries.py --source gisco [--cache DIR]

Writes registry/geometries/dk/{municipalities,zones}.geojson and .meta.yaml. The GISCO source also writes
the municipality entities in registry/entities/dk/entities.yaml (replaced wholesale); the DAGI source reads
them, so entity IDs stay the same and are mapped by kommunekode.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import numpy as np
import shapely
import yaml
from pyproj import Transformer
from shapely import wkt as shapely_wkt
from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from scanner import REPO_ROOT
from scanner.settings import get_settings

URL = "https://gisco-services.ec.europa.eu/distribution/v2/lau/geojson/LAU_RG_01M_2023_4326.geojson"
DAGI_URL = "https://graphql.datafordeler.dk/DAGI/v2"  # POST <url>?apiKey=...
DAGI_ENTITY = "DAGI_Kommuneinddeling"
DAGI_TERMS = "https://datafordeler.dk/vejledning/brugervilkaar/kds-geografiske-data/"
DAGI_PAGE_SIZE = 5  # full-resolution polygons are large; the service allows at most 1000
MAX_BYTES = 1_900_000  # municipalities.geojson budget (about 2 MB)
DAGI_TOLERANCES_DEG = (0.0001, 0.00015, 0.0002, 0.0003, 0.0005, 0.0008)  # tried in order until under MAX_BYTES
DAGI_PRECISION = 5  # decimals, about 1 m
TOLERANCE_DEG = 0.0006  # GISCO: about 65 m north-south; Douglas-Peucker, topology preserved
PRECISION = 4  # GISCO: decimals, about 11 m
# Christiansoe (411) is administered by the Ministry of Defence, not one of the 98 municipalities.
EXCLUDE = {"411"}
# Municipalities in DK2 (east of the Great Belt: Zealand, Lolland-Falster, Moen, Bornholm). All others,
# including Funen and the small islands Samsoe, Aeroe, Langeland, Laesoe, Fanoe, are DK1.
DK2 = {
    "101", "147", "151", "153", "155", "157", "159", "161", "163", "165", "167", "169", "173", "175",
    "183", "185", "187", "190", "201", "210", "217", "219", "223", "230", "240", "250", "253", "259",
    "260", "265", "269", "270", "306", "316", "320", "326", "329", "330", "336", "340", "350", "360",
    "370", "376", "390", "400",
}  # fmt: skip
OUT = REPO_ROOT / "registry" / "geometries" / "dk"
ENTITIES = REPO_ROOT / "registry" / "entities" / "dk" / "entities.yaml"


def slug(name: str) -> str:
    s = name.lower().replace("æ", "ae").replace("ø", "oe").replace("å", "aa")
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def rounded(coords: Any, precision: int = PRECISION) -> Any:
    if isinstance(coords[0], float):
        return [round(coords[0], precision), round(coords[1], precision)]
    return [rounded(c, precision) for c in coords]


def geojson_geometry(geom: Any, tolerance: float = TOLERANCE_DEG, precision: int = PRECISION) -> dict[str, Any]:
    g = mapping(geom.simplify(tolerance, preserve_topology=True))
    return {"type": g["type"], "coordinates": rounded(g["coordinates"], precision)}


def fc_text(feats: list[dict[str, Any]]) -> str:
    body = json.dumps({"type": "FeatureCollection", "features": feats}, ensure_ascii=False, separators=(",", ":"))
    return body + "\n"


def write_fc(path: Path, feats: list[dict[str, Any]]) -> None:
    path.write_text(fc_text(feats), encoding="utf-8")


# --- DAGI via Datafordeler GraphQL ---------------------------------------------------------------------


class DagiError(RuntimeError):
    """Raised with a message that is safe to print: it never contains the API key."""


def redact(text: str, key: str | None) -> str:
    """Remove the API key (raw and URL-encoded) and any apiKey= query value from text."""
    if key:
        text = text.replace(key, "<redacted>").replace(quote(key, safe=""), "<redacted>")
    return re.sub(r"(?i)(api[_-]?key=)[^&\s\"']+", r"\1<redacted>", text)


def dagi_query(after: str | None, now: str, geometry_field: str) -> str:
    cursor = f', after: "{after}"' if after else ""
    return (
        f'{{ {DAGI_ENTITY}(first: {DAGI_PAGE_SIZE}{cursor}, virkningstid: "{now}", registreringstid: "{now}") '
        f"{{ nodes {{ id_lokalId kommunekode {geometry_field} {{ crs wkt }} }} "
        "pageInfo { hasNextPage endCursor } } }"
    )


def _post(client: httpx.Client, url: str, key: str, query: str) -> bytes:
    """POST one GraphQL query. The key goes in the apiKey query parameter (the only form the docs show)."""
    try:
        r = client.post(url, params={"apiKey": key}, json={"query": query}, headers={"Accept": "application/json"})
    except httpx.HTTPError as e:
        raise DagiError(f"request to {url} failed: {redact(f'{type(e).__name__}: {e}', key)}") from None
    if r.status_code >= 400:
        hint = " (key not active yet? it can take about 15 minutes)" if r.status_code in (401, 403) else ""
        raise DagiError(f"HTTP {r.status_code} from {url}{hint}: {redact(r.text[:500], key)}")
    return r.content


def describe_entity(client: httpx.Client, url: str, key: str) -> str:
    """Best-effort introspection so a wrong field name can be fixed from the error output alone."""
    q = f'{{ __type(name: "{DAGI_ENTITY}") {{ fields {{ name }} }} }}'
    try:
        fields = json.loads(_post(client, url, key, q))["data"]["__type"]["fields"]
        return ", ".join(f["name"] for f in fields)
    except (DagiError, KeyError, TypeError, ValueError) as e:
        return f"(introspection failed: {redact(str(e), key)})"


def fetch_dagi(client: httpx.Client, url: str, key: str, geometry_field: str = "geometri") -> list[bytes]:
    """Fetch every page of DAGI_Kommuneinddeling valid now; returns the raw response bodies in order."""
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    pages: list[bytes] = []
    after: str | None = None
    while True:
        raw = _post(client, url, key, dagi_query(after, now, geometry_field))
        try:
            body = json.loads(raw)
        except ValueError:
            snippet = redact(raw[:300].decode("utf-8", "replace"), key)
            raise DagiError(f"non-JSON response from {url}: {snippet}") from None
        if body.get("errors"):
            msg = redact(json.dumps(body["errors"], ensure_ascii=False)[:1500], key)
            raise DagiError(
                f"GraphQL errors from {url}: {msg}\n"
                f"  fields on {DAGI_ENTITY}: {describe_entity(client, url, key)}\n"
                f"  geometry field tried: {geometry_field!r} (override with --dagi-geometry-field); "
                f"service version via --dagi-url (default {DAGI_URL})"
            )
        try:
            conn = body["data"][DAGI_ENTITY]
            nodes, info = conn["nodes"], conn["pageInfo"]
        except (KeyError, TypeError):
            raise DagiError(f"unexpected response shape: {redact(json.dumps(body)[:500], key)}") from None
        pages.append(raw)
        print(f"  page {len(pages)}: {len(nodes)} nodes", flush=True)
        if not info.get("hasNextPage"):
            return pages
        after = info["endCursor"]
        time.sleep(0.2)


def epsg_of(crs: str | int | None) -> int:
    """EPSG code from a DAGI crs value: the live service returns an integer (25832), docs show strings."""
    if isinstance(crs, int):
        return crs
    m = re.search(r"(\d{4,6})\s*$", crs or "")
    if not m:
        raise DagiError(f"cannot read an EPSG code from crs value {crs!r}")
    return int(m.group(1))


def reproject(geom: BaseGeometry, epsg: int) -> BaseGeometry:
    t = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)

    def fn(xy: Any) -> Any:
        x, y = t.transform(xy[:, 0], xy[:, 1])
        return np.column_stack([x, y])

    return shapely.transform(geom, fn)


def dagi_rows(pages: list[bytes], names: dict[str, str]) -> list[tuple[str, str, BaseGeometry]]:
    """Map DAGI nodes to (kommunekode, name, EPSG:4326 geometry) using the known kommunekode set `names`.

    DAGI codes are four digits ('0101'); the repository uses the integer form ('101'). Fails if any
    expected code is missing or an unexpected one (other than Christiansoe, 411) appears.
    """
    found: dict[str, BaseGeometry] = {}
    for raw in pages:
        for node in json.loads(raw)["data"][DAGI_ENTITY]["nodes"]:
            code = str(int(node["kommunekode"]))
            if code in EXCLUDE:
                continue
            geo = next((v for v in node.values() if isinstance(v, dict) and "wkt" in v), None)
            if geo is None or not geo["wkt"]:
                raise DagiError(f"kommunekode {code}: node has no geometry (keys: {sorted(node)})")
            if code in found:
                raise DagiError(f"kommunekode {code} returned twice (bitemporal versions not filtered?)")
            found[code] = reproject(shapely_wkt.loads(geo["wkt"]), epsg_of(geo.get("crs", "")))
    missing, extra = sorted(set(names) - set(found)), sorted(set(found) - set(names))
    if missing or extra:
        raise DagiError(f"kommunekode mismatch with registry: missing {missing}, unexpected {extra}")
    return [(c, names[c], found[c]) for c in sorted(found)]


def known_names() -> dict[str, str]:
    """kommunekode -> name from the existing municipality entities (keeps entity IDs stable)."""
    ents = (yaml.safe_load(ENTITIES.read_text(encoding="utf-8")) or {}).get("entities") or []
    names = {e["attributes"]["kommunekode"]: e["name"] for e in ents if e.get("kind") == "municipality"}
    if len(names) != 98:
        raise DagiError(f"{ENTITIES} lists {len(names)} municipalities, expected 98; run --source gisco first")
    return names


def fit_municipalities(feats_geoms: list[tuple[dict[str, Any], BaseGeometry]]) -> tuple[float, str]:
    """Smallest tolerance in DAGI_TOLERANCES_DEG whose GeoJSON is at most MAX_BYTES."""
    for tol in DAGI_TOLERANCES_DEG:
        feats = [f | {"geometry": geojson_geometry(g, tol, DAGI_PRECISION)} for f, g in feats_geoms]
        body = fc_text(feats)
        if len(body.encode("utf-8")) <= MAX_BYTES:
            return tol, body
    raise DagiError(f"municipalities.geojson exceeds {MAX_BYTES} bytes even at {DAGI_TOLERANCES_DEG[-1]} deg")


def zone_features(rows: list[tuple[str, str, BaseGeometry]], tolerance: float, precision: int) -> list[dict[str, Any]]:
    """Dissolve unsimplified municipality polygons per bidding zone, closing seams, then simplify."""
    out: list[dict[str, Any]] = []
    for zid in ("dk.zone.dk1", "dk.zone.dk2"):
        members = [g for code, _, g in rows if ("dk.zone.dk2" if code in DK2 else "dk.zone.dk1") == zid]
        u = unary_union([m.buffer(0) for m in members]).buffer(0.0002).buffer(-0.0002)  # close seams
        out.append({
            "type": "Feature", "properties": {"entity_id": zid, "municipalities": len(members)},
            "geometry": geojson_geometry(u, tolerance, precision),
        })  # fmt: skip
    return out


def municipality_entity(code: str, name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    eid = f"dk.municipality.{slug(name)}"
    zone = "dk.zone.dk2" if code in DK2 else "dk.zone.dk1"
    ent = {
        "id": eid, "kind": "municipality", "parent": zone, "name": name,
        "attributes": {"kommunekode": code}, "geometry_blob": None, "geometry_wkt": None,
    }  # fmt: skip
    feat = {"type": "Feature", "properties": {"entity_id": eid, "kommunekode": code, "name": name}}
    return ent, feat


def write_meta(name: str, meta: dict[str, Any], key: str | None) -> None:
    text = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=100)
    if key and key in text:
        raise DagiError("refusing to write meta file: it would contain the API key")
    (OUT / f"{name}.meta.yaml").write_text(text, encoding="utf-8")


def zones_meta(common: dict[str, Any], simplify: str) -> dict[str, Any]:
    return common | {
        "transformation": (
            "Dissolved (shapely unary_union) the unsimplified municipality polygons per bidding zone using the "
            "parent assignment in registry/entities/dk/entities.yaml, closed seams with a 0.0002 deg "
            f"buffer/unbuffer, then {simplify}. DK1 = Jutland, Funen and the "
            "islands Samsoe, Aeroe, Langeland, Laesoe, Fanoe; DK2 = Zealand, Lolland-Falster, Moen, Bornholm. "
            "Derived polygons approximate the zones; they are not an official TSO boundary."
        ),
    }


def build_dagi(url: str, geometry_field: str) -> int:
    secret = get_settings().datafordeler_api_key
    if secret is None or not secret.get_secret_value():
        print("DATAFORDELER_API_KEY is not set; use --source gisco or set the key", file=sys.stderr)
        return 1
    key = secret.get_secret_value()
    try:
        names = known_names()
        print(f"fetching {DAGI_ENTITY} from {url} (key redacted)")
        with httpx.Client(timeout=300, headers={"User-Agent": get_settings().user_agent}) as client:
            pages = fetch_dagi(client, url, key, geometry_field)
        sha = hashlib.sha256(b"".join(pages)).hexdigest()
        print("sha256", sha)
        rows = dagi_rows(pages, names)
        pairs = [(municipality_entity(c, n)[1], g) for c, n, g in rows]
        tol, body = fit_municipalities(pairs)
        (OUT / "municipalities.geojson").write_text(body, encoding="utf-8")
        write_fc(OUT / "zones.geojson", zone_features(rows, tol, DAGI_PRECISION))
        simplify = f"simplified (tolerance {tol} deg) and rounded to {DAGI_PRECISION} decimals as for municipalities"
        common = {
            "source_url": url,
            "source": (
                "Klimadatastyrelsen, Danmarks Administrative Geografiske Inddeling (DAGI), entity "
                f"{DAGI_ENTITY}, via Datafordeler GraphQL, full resolution, EPSG:25832"
            ),
            "licence": (
                "Free geographic data of Klimadatastyrelsen (terms: " + DAGI_TERMS + "). Attribution wording "
                "(unverified, confirm against the terms): 'Contains data from Klimadatastyrelsen (DAGI), "
                "retrieved via Datafordeler.'"
            ),
            "retrieved": datetime.now(UTC).date().isoformat(),
            "sha256_original": sha,
            "sha256_note": "SHA-256 of the concatenated raw GraphQL response bodies, in page order",
            "crs": "EPSG:4326",
        }
        meta_m = common | {
            "transformation": (
                "Queried DAGI_Kommuneinddeling valid now (virkningstid = registreringstid), paged "
                f"{DAGI_PAGE_SIZE} per request; dropped kommunekode 411 if present; WKT parsed and reprojected from "
                "the CRS reported by the service (EPSG:25832) to EPSG:4326 with pyproj (always_xy); shapely "
                f"simplify(tolerance={tol} deg, preserve_topology=True); coordinates rounded to {DAGI_PRECISION} "
                "decimals; kommunekode mapped to the existing dk.municipality.<slug> entity IDs and required to "
                "match the registry's 98 codes exactly. Built by scripts/build_geometries.py --source dagi."
            ),
            "notes": "Replaces the coarse GISCO 1:1M geometries. See docs/sources/dk/dagi.md.",
        }
        write_meta("municipalities", meta_m, key)
        write_meta("zones", zones_meta(common, simplify), key)
    except DagiError as e:
        print(f"DAGI build failed: {redact(str(e), key)}", file=sys.stderr)
        return 1
    print("ok", len(rows), "municipalities, tolerance", tol, "deg,", len(body.encode("utf-8")), "bytes")
    return 0


def build_gisco(cache: Path) -> int:
    cache.mkdir(parents=True, exist_ok=True)
    src = cache / "LAU_RG_01M_2023_4326.geojson"
    if not src.exists():
        with httpx.stream("GET", URL, timeout=300, follow_redirects=True) as r:
            r.raise_for_status()
            with src.open("wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
    raw = src.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    print("sha256", sha)
    data = json.loads(raw.decode("utf-8"))
    rows = [
        (f["properties"]["LAU_ID"], f["properties"]["LAU_NAME"], shape(f["geometry"]))
        for f in data["features"]
        if f["properties"]["CNTR_CODE"] == "DK" and f["properties"]["LAU_ID"] not in EXCLUDE
    ]
    if len(rows) != 98:
        print(f"expected 98 municipalities, got {len(rows)}", file=sys.stderr)
        return 1
    rows.sort(key=lambda t: t[0])
    ents: list[dict[str, Any]] = []
    feats: list[dict[str, Any]] = []
    for code, name, g in rows:
        ent, feat = municipality_entity(code, name)
        ents.append(ent)
        feats.append(feat | {"geometry": geojson_geometry(g)})
    write_fc(OUT / "municipalities.geojson", feats)
    write_fc(OUT / "zones.geojson", zone_features(rows, TOLERANCE_DEG, PRECISION))

    common = {
        "source_url": URL,
        "source": "Eurostat GISCO, Local Administrative Units (LAU) 2023, 1:1 million, EPSG:4326",
        "licence": (
            "GISCO statistical-unit dataset: free reuse incl. commercial, with attribution "
            "'(C) EuroGeographics for the administrative boundaries' / 'Source: Eurostat (GISCO)'. "
            "See https://ec.europa.eu/eurostat/web/gisco/geodata/administrative-units/local-administrative-units"
        ),
        "retrieved": datetime.now(UTC).date().isoformat(),
        "sha256_original": sha,
        "crs": "EPSG:4326",
    }
    meta_m = common | {
        "transformation": (
            "Filtered CNTR_CODE=DK, dropped LAU_ID 411 (Christiansoe, not one of the 98 municipalities); "
            f"shapely simplify(tolerance={TOLERANCE_DEG} deg, preserve_topology=True); coordinates rounded to "
            f"{PRECISION} decimals; LAU_ID kept as kommunekode. Built by scripts/build_geometries.py."
        ),
        "notes": (
            "Fallback source (--source gisco): DAGI (Dataforsyningen) needs a token and DAWA returned 410 Gone "
            "on retrieval day. GISCO LAU for Denmark is derived from the national municipality boundaries; the "
            "1:1M generalisation is coarse, suitable for display and point-in-polygon at municipality scale only."
        ),
    }
    simplify = "simplified and rounded as for municipalities"
    write_meta("municipalities", meta_m, None)
    write_meta("zones", zones_meta(common, simplify), None)

    existing = (yaml.safe_load(ENTITIES.read_text(encoding="utf-8")) or {}).get("entities") or []
    keep = [e for e in existing if e.get("kind") != "municipality"]
    ENTITIES.write_text(
        yaml.safe_dump({"entities": keep + ents}, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8"
    )
    print("ok", len(ents), "municipalities")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=("auto", "dagi", "gisco"), default="auto", help="auto: dagi if key set")
    ap.add_argument("--cache", type=Path, default=REPO_ROOT / ".data" / "geometry_cache", help="gisco download cache")
    ap.add_argument("--dagi-url", default=DAGI_URL)
    ap.add_argument("--dagi-geometry-field", default="geometri")
    args = ap.parse_args(argv)
    source = args.source
    if source == "auto":
        source = "dagi" if get_settings().datafordeler_api_key is not None else "gisco"
        print("source:", source)
    if source == "dagi":
        return build_dagi(args.dagi_url, args.dagi_geometry_field)
    return build_gisco(args.cache)


if __name__ == "__main__":
    raise SystemExit(main())
