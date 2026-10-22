"""Station rows, entity IDs and fact candidates from the capacity-map layers (WP1.5).

Dossiers: docs/sources/dk/dso-capacity-maps.md ("Loader"), docs/sources/dk/energinet-transmission-capacity.md.
Pure functions over the GeoJSON the FeatureServer returns with outSR=4326; nothing here fetches or
writes. Values are taken as published: MW stay MW, null stays null (never 0), a lower bound above
its upper bound is kept and reported, categories stay text.

Entity IDs (ADR 0004):
- TSO stations, capacity and free-bay layers: dk.substation.energinet.<slug(TSO_station_abbreviation)>,
  for example MAL_150 -> dk.substation.energinet.mal_150. The code is the station's identifier in both
  layers (rows with the same code share the entity); the names differ between layers (case, spelling),
  so they are not part of the ID.
- DSO stations: dk.substation.dso.<municipality>.<slug(DSO_station_abbreviation)>_<slug(DSO_station_name)>,
  for example dk.substation.dso.aarhus.has_hasle. Code and name alone are not unique (two stations
  "HAS Hasle", in Aarhus and on Bornholm). The TSO reference changes between publications and the DSO
  licence areas overlap, so neither belongs in a stable ID; the DAGI municipality containing the point
  does (its slug from the municipality entity ID), or `unlocated` when the point is in none.
- slug(): ae/oe/aa for æ/ø/å, other letters folded to ASCII, "$" -> "s", lower case, runs of anything
  else -> "_", trimmed. "$" is kept because 13 capacity-layer codes end in "--$" and are not the same as
  the bare code: BRA_150--$ is "Hedensted" there, BRA_150 is "Bredal" 2.5 km away in the free-bay layer.
  So BRA_150--$ -> bra_150_s and BRA_150 -> bra_150 stay two entities until a human approves a match.

Rows that map to the same entity are versions of it: the DSO layer keeps 26 stale rows for stations
republished under a new TSO reference (same code, name and point, older publication_date). Their
values become earlier facts (valid_from = their publication_date); the latest row gives the entity's
name, attributes and point. Two rows of one entity with the same publication date and different
values for a field are a conflict: neither value is used and the conflict is reported. Rows whose
source codes differ but slug to one entity are reported too (none on 2026-10-18).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from typing import Any, Literal

from scanner.core.registry import country_entity_id, slug

SOURCE = "kapacitetskort"
SERVICE_URL = "https://services7.arcgis.com/an1TuZOdFp9M3PVx/arcgis/rest/services/Kapacitetskortet_V2/FeatureServer"
BOUNDARY_SERVICE_URL = (
    "https://services7.arcgis.com/an1TuZOdFp9M3PVx/arcgis/rest/services/Netselskabsgraenser/FeatureServer"
)
BOUNDARY_LAYER = "Netselskabsgraenser"
COUNTRY = "dk"
UNLOCATED = "unlocated"


class ParseError(ValueError):
    """The layer does not have the shape this module expects (missing field, bad geometry)."""


@dataclass(frozen=True)
class FactField:
    """A source field written as a fact. select: (field, value) the row must have, e.g. a grid precondition."""

    field: str
    entry_id: str
    unit: str | None = "MW"
    text: bool = False
    select: tuple[str, str] | None = None


@dataclass(frozen=True)
class Layer:
    name: str
    level: Literal["dso", "tso"]
    code_field: str
    name_field: str
    facts: tuple[FactField, ...]
    attribute_fields: Mapping[str, str]  # entity attribute -> source field (latest row wins)
    bounds: tuple[tuple[str, str], ...] = ()  # (lower, upper) field pairs checked for lower > upper

    @property
    def required_fields(self) -> set[str]:
        sel = {f.select[0] for f in self.facts if f.select}
        return {self.code_field, self.name_field, "publication_date", *sel, *self.attribute_fields.values()} | {
            f.field for f in self.facts
        }


def _entry(layer: str, suffix: str) -> str:
    return f"dk.kapacitetskort.{layer}.{suffix}"


DSO_LAYER = "DSO_stations_capacity_points"
TSO_LAYER = "TSO_stations_capacity_points"
BAY_LAYER = "TSO_stations_fields_points"

_CONS_LB = "available_capacity_consumption_with_limited_grid_access_MW_lower"
_CONS_UB = "available_capacity_consumption_with_limited_grid_access_MW_upper"

DSO = Layer(
    name=DSO_LAYER,
    level="dso",
    code_field="DSO_station_abbreviation",
    name_field="DSO_station_name",
    facts=tuple(
        FactField(f, _entry(DSO_LAYER, suffix))
        for f, suffix in (
            ("available_capacity_consumption_MW_DSO_station_without_TSO_transf", "cap_cons_full_no_tso"),
            ("available_capacity_consumption_MW_DSO_station_with_TSO_transform", "cap_cons_full_with_tso"),
            ("available_capacity_production_MW_DSO_station_without_TSO_transfo", "cap_prod_full_no_tso"),
            ("available_capacity_production_MW_DSO_station_with_TSO_transforme", "cap_prod_full_with_tso"),
            (_CONS_LB, "cap_cons_limited_lb"),
            (_CONS_UB, "cap_cons_limited_ub"),
            ("available_capacity_solar_MW_lower_bound", "cap_solar_limited_lb"),
            ("available_capacity_solar_MW_upper_bound", "cap_solar_limited_ub"),
            ("available_capacity_wind_MW_lower_bound", "cap_wind_limited_lb"),
            ("available_capacity_wind_MW_upper_bound", "cap_wind_limited_ub"),
            ("connected_solar_MW", "connected_solar"),
            ("connected_wind_MW", "connected_wind"),
            ("connected_consumption_with_limited_grid_access", "connected_cons_limited"),
        )
    ),
    attribute_fields={"tso_station_abbreviation": "TSO_station_abbreviation", "voltage_kv": "voltage_level_kv"},
    bounds=(
        (_CONS_LB, _CONS_UB),
        ("available_capacity_solar_MW_lower_bound", "available_capacity_solar_MW_upper_bound"),
        ("available_capacity_wind_MW_lower_bound", "available_capacity_wind_MW_upper_bound"),
    ),
)
TSO = Layer(
    name=TSO_LAYER,
    level="tso",
    code_field="TSO_station_abbreviation",
    name_field="TSO_station_name",
    facts=tuple(
        FactField(f, _entry(TSO_LAYER, f"{f}.{grid}"), select=("grid_precondition", grid))
        for f in ("available_capacity_production_MW", "available_capacity_consumption_MW")
        for grid in ("existing_grid", "reinforced_grid")
    ),
    attribute_fields={"voltage_kv": "voltage_level_kv"},
)
BAY = Layer(
    name=BAY_LAYER,
    level="tso",
    code_field="TSO_station_abbreviation",
    name_field="TSO_station_name",
    facts=(
        FactField(
            "available_fields_for_publication_capacitymap",
            _entry(BAY_LAYER, "available_fields_for_publication_capacitymap"),
            unit=None,
            text=True,
        ),
    ),
    attribute_fields={"station_status": "station_status", "area_type": "consumption_or_production_area"},
)
# Order matters: an entity in several layers takes its name and point from the first layer it appears in.
LAYERS: tuple[Layer, ...] = (DSO, TSO, BAY)


def publication_time(value: Any) -> datetime | None:
    """valid_from of a row: epoch milliseconds (UTC) as published, or a YYYY-MM-DD date string at 00:00 UTC
    (the free-bay layer), matching the epoch values of the other layers, which are UTC midnights."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ParseError(f"publication_date {value!r} is not a date")
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value / 1000, UTC)
    if isinstance(value, str):
        try:
            return datetime.combine(date.fromisoformat(value), time(), UTC)
        except ValueError as exc:
            raise ParseError(f"publication_date {value!r} is not a date") from exc
    raise ParseError(f"publication_date {value!r} is not a date")


@dataclass(frozen=True)
class StationRow:
    layer: Layer
    code: str
    name: str
    lon: float
    lat: float
    published: datetime | None
    props: Mapping[str, Any]
    document_id: str

    @property
    def point(self) -> tuple[float, float]:
        return (self.lon, self.lat)


def features(fc: Any) -> list[dict[str, Any]]:
    if not isinstance(fc, dict) or fc.get("type") != "FeatureCollection" or not isinstance(fc.get("features"), list):
        raise ParseError("not a GeoJSON FeatureCollection")
    crs = str(((fc.get("crs") or {}).get("properties") or {}).get("name", "EPSG:4326"))
    if not crs.endswith(("4326", "CRS84")):
        raise ParseError(f"crs {crs} is not EPSG:4326 (query with outSR=4326)")
    return list(fc["features"])


def parse_layer(layer: Layer, fc: Any, document_id: str) -> list[StationRow]:
    """Rows of one layer page. Raises ParseError when a field the layer needs is missing (schema change)."""
    rows = []
    for i, feat in enumerate(features(fc)):
        props = feat.get("properties") or {}
        missing = layer.required_fields - set(props)
        if missing:
            raise ParseError(f"{layer.name} feature {i}: fields missing: {sorted(missing)}")
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates")
        if geom.get("type") != "Point" or not isinstance(coords, list) or len(coords) < 2:
            raise ParseError(f"{layer.name} feature {i}: geometry is not a point")
        lon, lat = float(coords[0]), float(coords[1])
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise ParseError(f"{layer.name} feature {i}: ({lon}, {lat}) is outside EPSG:4326")
        code, name = props[layer.code_field], props[layer.name_field]
        if not isinstance(code, str) or not code.strip() or not isinstance(name, str):
            raise ParseError(f"{layer.name} feature {i}: station code or name missing")
        rows.append(
            StationRow(layer, code, name, lon, lat, publication_time(props["publication_date"]), props, document_id)
        )
    return rows


@dataclass(frozen=True)
class Location:
    """Polygons that contain a station's point, from the runtime's spatial join (entity IDs)."""

    zones: tuple[str, ...] = ()
    municipalities: tuple[str, ...] = ()
    dso_areas: tuple[str, ...] = ()


def entity_id(row: StationRow, loc: Location) -> str:
    if row.layer.level == "tso":
        return f"dk.substation.energinet.{slug(row.code)}"
    municipality = loc.municipalities[0].rsplit(".", 1)[-1] if len(loc.municipalities) == 1 else UNLOCATED
    return f"dk.substation.dso.{municipality}.{slug(row.code)}_{slug(row.name)}"


def _number(row: StationRow, f: FactField) -> float | str | None:
    v = row.props[f.field]
    if v is None:
        return None
    if f.text:
        if not isinstance(v, str):
            raise ParseError(f"{row.layer.name} {row.code}: {f.field} is {v!r}, expected text")
        return v
    if isinstance(v, bool) or not isinstance(v, int | float):
        raise ParseError(f"{row.layer.name} {row.code}: {f.field} is {v!r}, expected a number")
    return float(v)


@dataclass(frozen=True)
class Candidate:
    entity_id: str
    entry_id: str
    value: float | str | None
    unit: str | None
    valid_from: datetime | None
    document_id: str


@dataclass(frozen=True)
class Quirk:
    entity_id: str
    lower_field: str
    upper_field: str
    lower: float
    upper: float
    valid_from: datetime | None


@dataclass
class StationEntity:
    entity_id: str
    name: str
    parent_id: str
    lon: float
    lat: float
    geometry_source: str
    attributes: dict[str, Any]


@dataclass
class Stations:
    entities: dict[str, StationEntity] = field(default_factory=dict)
    candidates: list[Candidate] = field(default_factory=list)
    quirks: list[Quirk] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    id_collisions: list[str] = field(default_factory=list)  # different source codes, one entity
    rows_per_layer: dict[str, int] = field(default_factory=dict)
    entities_per_layer: dict[str, int] = field(default_factory=dict)


def build_stations(rows: Iterable[StationRow], locations: Mapping[tuple[float, float], Location]) -> Stations:
    """Group rows into entities (see the module docstring) and list their fact candidates and quirks.

    locations: the spatial join for every row's point. parent is the zone when the point lies in exactly
    one zone, else the country.
    """
    out = Stations()
    by_entity: dict[str, list[StationRow]] = {}
    for r in rows:
        out.rows_per_layer[r.layer.name] = out.rows_per_layer.get(r.layer.name, 0) + 1
        by_entity.setdefault(entity_id(r, locations[r.point]), []).append(r)
    for eid, group in by_entity.items():
        codes = {(r.code, r.name if r.layer.level == "dso" else "") for r in group}
        if len(codes) > 1:
            out.id_collisions.append(f"{eid}: {sorted(codes)}")
        for layer in LAYERS:
            mine = [r for r in group if r.layer is layer]
            if not mine:
                continue
            out.entities_per_layer[layer.name] = out.entities_per_layer.get(layer.name, 0) + 1
            latest = max(mine, key=lambda r: r.published or datetime.min.replace(tzinfo=UTC))
            _merge_entity(out, eid, latest, locations[latest.point], mine)
            _candidates(out, eid, layer, mine)
    return out


def _merge_entity(out: Stations, eid: str, latest: StationRow, loc: Location, mine: Sequence[StationRow]) -> None:
    layer = latest.layer
    attrs: dict[str, Any] = {k: latest.props[f] for k, f in layer.attribute_fields.items()}
    if layer is TSO:
        attrs["grid_preconditions"] = sorted({str(r.props["grid_precondition"]) for r in mine})
    ent = out.entities.get(eid)
    if ent is None:
        zone = loc.zones[0] if len(loc.zones) == 1 else None
        base: dict[str, Any] = {
            "source": SOURCE,
            "layers": [],
            "source_station_id": latest.code,
            "zone": zone,
            "municipality": loc.municipalities[0] if len(loc.municipalities) == 1 else None,
            "listed": True,
        }
        if layer.level == "dso":
            base["dso"] = loc.dso_areas[0] if len(loc.dso_areas) == 1 else None
            if len(loc.dso_areas) > 1:
                base["dso_candidates"] = list(loc.dso_areas)
        ent = StationEntity(
            eid, latest.name, zone or country_entity_id(COUNTRY), latest.lon, latest.lat, f"{SOURCE}:{layer.name}", base
        )
        out.entities[eid] = ent
    ent.attributes["layers"].append(layer.name)
    for k, v in attrs.items():
        ent.attributes.setdefault(k, v)


def _candidates(out: Stations, eid: str, layer: Layer, mine: Sequence[StationRow]) -> None:
    for f in layer.facts:
        rows = [r for r in mine if f.select is None or r.props.get(f.select[0]) == f.select[1]]
        by_time: dict[datetime | None, list[StationRow]] = {}
        for r in rows:
            by_time.setdefault(r.published, []).append(r)
        for published, same in by_time.items():
            values = {_number(r, f) for r in same}
            if len(values) > 1:
                out.conflicts.append(f"{eid} {f.entry_id} at {published}: {sorted(map(repr, values))}")
                continue
            out.candidates.append(Candidate(eid, f.entry_id, values.pop(), f.unit, published, same[0].document_id))
    for lo, hi in layer.bounds:
        for r in mine:
            a, b = r.props[lo], r.props[hi]
            if isinstance(a, int | float) and isinstance(b, int | float) and a > b:
                out.quirks.append(Quirk(eid, lo, hi, float(a), float(b), r.published))


@dataclass(frozen=True)
class DsoArea:
    entity_id: str
    name: str
    geometry: dict[str, Any]
    attributes: dict[str, Any]


def parse_dso_areas(fc: Any) -> list[DsoArea]:
    """DSO licence areas (Netselskabsgraenser) as dso_area entities: dk.dso_area.<slug(Navn_kort)>."""
    out: dict[str, DsoArea] = {}
    for i, feat in enumerate(features(fc)):
        p = feat.get("properties") or {}
        geom = feat.get("geometry") or {}
        if geom.get("type") not in ("Polygon", "MultiPolygon"):
            raise ParseError(f"{BOUNDARY_LAYER} feature {i}: geometry is not a polygon")
        name, short = p.get("Selsk_Nvn"), p.get("Navn_kort") or p.get("Selsk_Nvn")
        if not isinstance(name, str) or not isinstance(short, str):
            raise ParseError(f"{BOUNDARY_LAYER} feature {i}: Selsk_Nvn missing")
        eid = f"{COUNTRY}.dso_area.{slug(short)}"
        if eid in out:
            raise ParseError(f"{BOUNDARY_LAYER}: two licence areas map to {eid}")
        attrs = {
            "source": SOURCE,
            "layers": [BOUNDARY_LAYER],
            "selsk_nr": p.get("Selsk_Nr"),
            "cvr": p.get("CVR"),
            "short_name": p.get("Navn_kort"),
            "listed": True,
        }
        out[eid] = DsoArea(eid, name, geom, attrs)
    return list(out.values())
