"""Adapter manifests: what to fetch, when, and what it becomes (docs/architecture.md)."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from scanner.core.registry import Country, load_countries
from scanner.core.sensitivity import Sensitivity
from scanner.core.time import parse_duration

# `resolution` value for sources whose every period carries its own resolution (ENTSO-E): series take
# the resolution of their records, and completeness is counted per series rather than per manifest.
PER_PERIOD = "per_period"
# `zones_from_registry` scopes: which countries' zones (those with a verified EIC) a base-tier manifest covers.
ZoneScope = Literal["active", "active_and_reference", "all"]
_SCOPE_STATUSES: dict[str, tuple[str, ...]] = {
    "active": ("active",),
    "active_and_reference": ("active", "reference"),
    "all": ("active", "reference", "draft"),
}


class FingerprintConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    warmup_runs: int = 14
    null_rate_margin: float = 0.05
    iqr_multiplier: float = 3.0


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    source_short: str = Field(pattern=r"^[a-z0-9_]+$")
    dataset: str
    country: str
    adapter: str = Field(pattern=r"^[a-z0-9_.]+:[A-Za-z0-9_]+$")
    status: Literal["active", "backfill_only", "draft", "retired"]
    time_field_utc: str
    zone_field: str | None
    # Source zone code -> zone entity ID. Filled from the registry when zones_from_registry is set.
    zones: dict[str, str]
    # Base tier (ADR 0011): zones are every zone with a verified EIC in countries of this scope, keyed
    # by EIC, so adding a country is a registry change only. `zones` must then be empty in the file.
    zones_from_registry: ZoneScope | None = None
    # Source records per interval per zone (e.g. 3 when one record per ForecastType). Used for completeness.
    records_per_interval_per_zone: int = Field(default=1, ge=1)
    # ISO 8601 duration, or PER_PERIOD.
    resolution: str
    history_start_local: date
    # Inclusive: the last local day the source publishes.
    history_end_local: date | None = None
    schedule: list[str] = []
    schedule_timezone: str
    # How far before the partition day each run re-fetches, to catch revisions.
    lookback: str
    # Window of the frequent (manifest `schedule`) ticks: they fetch from this long before the tick instead of
    # from the partition day minus `lookback`. The 01:30 yesterday run keeps the full lookback and so catches
    # late revisions. None: every tick uses the full lookback.
    recent_lookback: str | None = None
    # How far past the partition day each run parses and writes, for data published ahead of its
    # interval (next-day prices, forecasts). The fingerprint still covers the partition day only.
    lookahead: str = "P0D"
    freshness_sla: str | None
    entries: list[str]
    sensitivity: Sensitivity
    licence: str
    fingerprint: FingerprintConfig = FingerprintConfig()
    notes: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Manifest:
        durations = [self.lookback, self.lookahead] + ([] if self.per_period_resolution else [self.resolution])
        if self.recent_lookback is not None:
            if parse_duration(self.recent_lookback) <= timedelta(0):
                raise ValueError(f"{self.dataset}: recent_lookback must be positive")
            if parse_duration(self.recent_lookback) > parse_duration(self.lookback):
                raise ValueError(f"{self.dataset}: recent_lookback must not exceed lookback")
        for d in durations:
            if parse_duration(d) < timedelta(0):
                raise ValueError(f"{self.dataset}: duration {d!r} must not be negative")
        if self.zone_field is None and len(self.zones) != 1:
            raise ValueError(f"{self.dataset}: without a zone_field the manifest must name exactly one zone")
        if self.zones_from_registry and not self.zone_field:
            raise ValueError(f"{self.dataset}: zones_from_registry needs a zone_field holding the EIC")
        if self.freshness_sla:
            parse_duration(self.freshness_sla)
        for cron in self.schedule:
            if len(cron.split()) != 5:
                raise ValueError(f"{self.dataset}: schedule {cron!r} is not a 5-field cron")
        if self.status == "active" and not self.schedule:
            raise ValueError(f"{self.dataset}: active manifests need a schedule")
        if self.status == "backfill_only" and self.history_end_local is None:
            raise ValueError(f"{self.dataset}: backfill_only manifests need history_end_local")
        if not self.entries:
            raise ValueError(f"{self.dataset}: no codebook entries")
        return self

    @property
    def per_period_resolution(self) -> bool:
        return self.resolution == PER_PERIOD

    @property
    def resolution_td(self) -> timedelta:
        if self.per_period_resolution:
            raise ValueError(f"{self.dataset}: resolution varies per source period; use each series' own")
        return parse_duration(self.resolution)

    @property
    def lookback_td(self) -> timedelta:
        return parse_duration(self.lookback)

    @property
    def recent_lookback_td(self) -> timedelta | None:
        return None if self.recent_lookback is None else parse_duration(self.recent_lookback)

    @property
    def lookahead_td(self) -> timedelta:
        return parse_duration(self.lookahead)

    @property
    def asset_name(self) -> str:
        return f"{self.country}_{self.source_short}_{self.dataset}".lower()


def manifest_paths(root: Path) -> list[Path]:
    return sorted((root / "src" / "scanner" / "adapters").glob("*/manifests/*.yaml"))


def registry_zones(countries: dict[str, Country], scope: ZoneScope) -> dict[str, str]:
    """EIC -> zone ID for every zone with a verified EIC in countries whose status is in scope."""
    statuses = _SCOPE_STATUSES[scope]
    return {
        z.eic: z.id
        for c in countries.values()
        if c.status in statuses
        for z in c.zones
        if z.eic is not None and z.eic_verified
    }


def load_manifests(root: Path) -> list[Manifest]:
    manifests = [Manifest(**yaml.safe_load(p.read_text())) for p in manifest_paths(root)]
    if not any(m.zones_from_registry for m in manifests):
        return manifests
    countries = load_countries(root)
    out = []
    for m in manifests:
        if m.zones_from_registry:
            if m.zones:
                raise ValueError(f"{m.dataset}: zones come from the registry; leave `zones` empty")
            m = m.model_copy(update={"zones": registry_zones(countries, m.zones_from_registry)})
        out.append(m)
    return out
