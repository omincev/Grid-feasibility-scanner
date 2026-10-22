"""Manifest model extensions for the base tier: zones from the registry and per-period resolution."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from scanner.core.manifest import PER_PERIOD, Manifest, registry_zones
from scanner.core.registry import Country


def _manifest(**changes: Any) -> Manifest:
    base: dict[str, Any] = {
        "source": "entsoe",
        "source_short": "entsoe",
        "dataset": "EnergyPrices",
        "country": "eu",
        "adapter": "scanner.adapters.entsoe:EntsoeAdapter",
        "status": "draft",
        "time_field_utc": "interval_start",
        "zone_field": "in_domain",
        "zones": {},
        "zones_from_registry": "active",
        "resolution": PER_PERIOD,
        "history_start_local": date(2025, 3, 4),
        "schedule_timezone": "Europe/Brussels",
        "lookback": "P1D",
        "freshness_sla": None,
        "entries": ["eu.entsoe.EnergyPrices.price_amount"],
        "sensitivity": "public",
        "licence": "x",
    }
    return Manifest(**{**base, **changes})


def _country(code: str, status: str, zones: list[tuple[str, str | None, bool]]) -> Country:
    return Country(
        code=code,
        name=code,
        status=status,  # type: ignore[arg-type]
        timezone="UTC",
        currency="EUR",
        reporting_currency="EUR",
        tso=None,
        geography_levels=["zone"],
        zones=[
            {"id": f"{code}.zone.{z}", "short": z, "name": z, "eic": eic, "eic_verified": ok}  # type: ignore[misc]
            for z, eic, ok in zones
        ],
        precedence={},
        reference_zones=[],
    )


def test_per_period_resolution() -> None:
    m = _manifest()
    assert m.per_period_resolution
    with pytest.raises(ValueError, match="varies per source period"):
        _ = m.resolution_td
    assert _manifest(resolution="PT15M", zones_from_registry=None, zones={"DK1": "dk.zone.dk1"}).resolution_td


def test_zones_from_registry_needs_a_zone_field() -> None:
    with pytest.raises(ValidationError, match="zone_field"):
        _manifest(zone_field=None)


def test_no_zone_field_needs_exactly_one_zone() -> None:
    one = {"zones": {"DK1": "dk.zone.dk1"}, "zones_from_registry": None}
    assert _manifest(zone_field=None, **one).zones == {"DK1": "dk.zone.dk1"}
    for zones in ({}, {"DK1": "dk.zone.dk1", "DK2": "dk.zone.dk2"}):
        with pytest.raises(ValidationError, match="exactly one zone"):
            _manifest(zone_field=None, zones_from_registry=None, zones=zones)


def test_registry_zones_by_scope_and_verification() -> None:
    countries = {
        "aa": _country("aa", "active", [("a1", "10YAA-1--------1", True), ("a2", "10YAA-2--------2", False)]),
        "bb": _country("bb", "reference", [("b1", "10YBB----------1", True)]),
        "cc": _country("cc", "draft", [("c1", "10YCC----------1", True), ("c2", None, False)]),
    }
    assert registry_zones(countries, "active") == {"10YAA-1--------1": "aa.zone.a1"}
    assert set(registry_zones(countries, "active_and_reference").values()) == {"aa.zone.a1", "bb.zone.b1"}
    assert set(registry_zones(countries, "all").values()) == {"aa.zone.a1", "bb.zone.b1", "cc.zone.c1"}
