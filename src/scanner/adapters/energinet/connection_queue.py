"""Energinet's connection-queue map: Power BI querydata response -> queue facts per region.

Dossier: docs/sources/dk/energinet-connection-queue.md. The snapshot `dk.energinet.connection_queue`
POSTs one grouped query (Region x Type x Fasenavn with the measures "Kapacitet i alt MW" and
"Senest opdateret"); this module decodes the answer. Pure functions.

Entity level: one `tso_area` entity per region the report groups by, dk.tso_area.<slug(region)>
(for example dk.tso_area.region_hovedstaden). The queue is published per region, so region entities
keep one numeric fact per (entity, attribute) and the append-only change detection unchanged; a zone
or country entity would need the region inside value_json. `tso_area` is the closest ADR 0004 kind:
the areas Energinet, the TSO, reports its queue by. Parent: the bidding zone containing the region
(Danish regions nest in the zones: DK1 = Jutland and Funen, DK2 = Zealand, Lolland-Falster and
Bornholm, as in registry/geometries/dk/zones.meta.yaml); a region not in REGION_ZONES gets the
country as parent and is reported.

valid_from is the report's "Senest opdateret" date at 00:00 UTC, the convention of the capacity-map
publication dates. Padding rows (empty phase or type) are skipped; an empty measure stays null.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any

from scanner.core.registry import country_entity_id, slug

SOURCE = "energinet.connection_queue"
COUNTRY = "dk"
CAPACITY = "m.Kapacitet i alt MW"
UPDATED = "m.Senest opdateret"
REGION, TYPE, PHASE = "p.Region", "t.Type", "f.Fasenavn"

_ENTRY = "dk.energinet.connection_queue.Kapacitet_i_alt_MW"
ENTRIES: dict[tuple[str, str], str] = {
    (t, p): f"{_ENTRY}.{direction}_{phase}"
    for t, direction in (("Forbrug", "consumption"), ("Produktion", "production"))
    for p, phase in (("Screening", "screening"), ("Modning", "maturation"), ("Etablering", "establishment"))
}
REGION_ZONES = {
    "Region Nordjylland": "dk.zone.dk1",
    "Region Midtjylland": "dk.zone.dk1",
    "Region Syddanmark": "dk.zone.dk1",
    "Region Sjælland": "dk.zone.dk2",
    "Region Hovedstaden": "dk.zone.dk2",
}


class ParseError(ValueError):
    """The response does not have the expected shape."""


_EMBED = re.compile(r"app\.powerbi\.com/view\?r=([A-Za-z0-9+/=_-]+)")


def powerbi_resource_key(html: str) -> str:
    """The public resource key (`k`) from the first Power BI publish-to-web embed URL in a page."""
    m = _EMBED.search(html)
    if not m:
        raise ParseError("no app.powerbi.com/view?r= embed URL in the page")
    token = m.group(1)
    try:
        info = json.loads(base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_"))
    except (binascii.Error, ValueError) as exc:
        raise ParseError("embed URL parameter r is not base64 JSON") from exc
    key = info.get("k") if isinstance(info, dict) else None
    if not isinstance(key, str) or not key:
        raise ParseError("embed URL parameter r has no resource key")
    return key


def decode_dsr(response: Any) -> tuple[list[str], list[list[Any]]]:
    """Column names (descriptor Select names) and rows of the first data shape of a querydata response.

    Power BI's compressed DSR rows: `C` lists the values of the columns that are neither repeated (bit i
    of `R`: same as the previous row) nor null (bit i of `Ø`); columns with a value dictionary (`DN`)
    hold an index into ValueDicts.
    """
    try:
        data = response["results"][0]["result"]["data"]
        names = {s["Value"]: s["Name"] for s in data["descriptor"]["Select"]}
        ds = data["dsr"]["DS"][0]
        raw = ds["PH"][0]["DM0"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ParseError(f"unexpected querydata response shape: {exc!r}") from exc
    if not raw:
        return [], []
    schema = raw[0]["S"]
    dicts = ds.get("ValueDicts", {})
    previous: list[Any] = [None] * len(schema)
    rows: list[list[Any]] = []
    for r in raw:
        repeat, null, values = r.get("R", 0), r.get("Ø", 0), iter(r.get("C", []))
        row: list[Any] = []
        for i, col in enumerate(schema):
            if repeat >> i & 1:
                v = previous[i]
            elif null >> i & 1:
                v = None
            else:
                try:
                    v = next(values)
                except StopIteration as exc:
                    raise ParseError(f"DSR row has too few values: {r}") from exc
                if "DN" in col and isinstance(v, int):
                    v = dicts[col["DN"]][v]
            row.append(v)
        rows.append(row)
        previous = row
    return [names[c["N"]] for c in schema], rows


@dataclass(frozen=True)
class QueueCell:
    region: str
    entry_id: str
    mw: float | None


def _mw(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int | float | str):
        raise ParseError(f"capacity {v!r} is not a number")
    try:
        return float(v)
    except ValueError as exc:
        raise ParseError(f"capacity {v!r} is not a number") from exc


def queue_cells(response: Any) -> tuple[list[QueueCell], datetime | None]:
    """One cell per region, direction and phase, and the report's last-updated date (valid_from)."""
    columns, rows = decode_dsr(response)
    missing = {REGION, TYPE, PHASE, CAPACITY} - set(columns)
    if missing:
        raise ParseError(f"querydata response lacks columns {sorted(missing)}")
    at = {c: i for i, c in enumerate(columns)}
    cells: dict[tuple[str, str], QueueCell] = {}
    updated: set[str] = set()
    for row in rows:
        if UPDATED in at and row[at[UPDATED]] is not None:
            updated.add(str(row[at[UPDATED]]))
        region, kind, phase = row[at[REGION]], row[at[TYPE]], row[at[PHASE]]
        if not kind or not phase:
            continue  # Power BI padding rows
        entry = ENTRIES.get((kind, phase))
        if entry is None or not isinstance(region, str) or not region:
            raise ParseError(f"unexpected queue cell: region={region!r} type={kind!r} phase={phase!r}")
        if (region, entry) in cells:
            raise ParseError(f"duplicate queue cell {region} {kind} {phase}")
        cells[(region, entry)] = QueueCell(region, entry, _mw(row[at[CAPACITY]]))
    return list(cells.values()), _updated_date(updated)


def _updated_date(texts: set[str]) -> datetime | None:
    dates = set()
    for t in texts:
        m = re.search(r"(\d{2})-(\d{2})-(\d{4})", t)
        if not m:
            raise ParseError(f"cannot read a DD-MM-YYYY date from {t!r}")
        day, month, year = map(int, m.groups())
        dates.add(date(year, month, day))
    if len(dates) > 1:
        raise ParseError(f"several last-updated dates: {sorted(dates)}")
    return datetime.combine(dates.pop(), time(), UTC) if dates else None


def region_entity(region: str) -> tuple[str, str]:
    """(entity ID, parent ID) of a queue region."""
    return f"{COUNTRY}.tso_area.{slug(region)}", REGION_ZONES.get(region, country_entity_id(COUNTRY))
