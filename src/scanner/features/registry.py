"""Every feature declares its level, accepted equivalence classes and phase (ADR 0012).

`make contracts` checks that accepted classes exist. Feature assets must only read series whose
codebook entry belongs to one of the accepted classes; `scanner.features.inputs.load_input` enforces it.

This module also declares, for the zone features built so far (docs/features.md):
- FEATURE_INPUTS: which codebook class, and which entries of it, each input reads
- `outputs` and `regime_rules` on each FeatureSpec: the feature keys written to feat.feature_value and
  the market rule keys whose `valid_from` dates split the analysis window into regime sub-windows

Which statistic feeds which score, and in which direction, is a scenario choice, not declared here: the
`inputs` parameter of the scenario's score_method set (assumptions/score_method_v0.yaml,
scanner.scores.method).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

Level = Literal["zone", "substation", "municipality", "site"]

# docs/features.md: the MVP analysis window starts 2025-03-04 00:00 local time (ImbalancePrice start).
ANALYSIS_START_LOCAL = date(2025, 3, 4)

# F1 inter-zone spread: absolute difference of the first minus the second zone, written to both.
INTERZONE_PAIRS: tuple[tuple[str, str], ...] = (("dk.zone.dk1", "dk.zone.dk2"),)


@dataclass(frozen=True)
class FeatureSpec:
    key: str
    title: str
    level: Level
    accepted_classes: tuple[str, ...]
    phase: int
    score_weight_key: str | None
    doc_anchor: str
    outputs: tuple[str, ...] = ()
    regime_rules: tuple[str, ...] = ()


@dataclass(frozen=True)
class InputSpec:
    """One input of a feature.

    equivalence_class: the codebook class of the input; must be one of the feature's accepted classes.
    entries: codebook entry IDs to read, each of which must belong to the class. Empty means any entry
        of the class. Per entity, one source is used (the country file's precedence decides between
        several) and its series must not overlap in time (scanner.features.inputs).
    vintage: how one forecast vintage per interval is chosen; required for inputs with issue times.
        'latest_issued_before_start' keeps the latest issue at or before interval_start.
    resolution_rule: market rule key giving the resolution in force per local day. Where the market rules
        of the entity's country define it, only rows whose interval length equals the rule's value are kept
        (ADR 0002); elsewhere the disjoint series of the chosen source are simply concatenated.
    """

    equivalence_class: str
    entries: tuple[str, ...] = ()
    vintage: Literal["latest_issued_before_start"] | None = None
    resolution_rule: str | None = None


DAY_AHEAD_PRICE = InputSpec(
    "day_ahead_price",
    (
        "dk.eds.Elspotprices.SpotPriceEUR",
        "dk.eds.DayAheadPrices.DayAheadPriceEUR",
        # Base tier (ADR 0011): every zone with a verified EIC; where EDS covers a zone too, the
        # country file's precedence decides.
        "eu.entsoe.EnergyPrices.price_amount",
    ),
    resolution_rule="day_ahead.resolution",
)


def _forecast_day_ahead(tech: str) -> InputSpec:
    return InputSpec(
        f"generation_forecast.{tech}",
        (f"dk.eds.Forecasts_Hour.ForecastDayAhead.{tech}",),
        vintage="latest_issued_before_start",
    )


# TODO(WP1.3): actuals are read by class until the production/consumption dataset lands; name its
# entries here if it maps more than one entry per class and zone (loading then refuses to guess).
_GEN_ACTUAL = {f"actual_{t}": InputSpec(f"generation_actual.{t}") for t in ("solar", "wind_onshore", "wind_offshore")}

FEATURE_INPUTS: dict[str, dict[str, InputSpec]] = {
    "f1_price_spread": {"price": DAY_AHEAD_PRICE},
    "f2_curtailment": {
        "price": DAY_AHEAD_PRICE,
        "forecast_solar": _forecast_day_ahead("solar"),
        "forecast_wind_onshore": _forecast_day_ahead("wind_onshore"),
        "forecast_wind_offshore": _forecast_day_ahead("wind_offshore"),
        **_GEN_ACTUAL,
    },
    "f4_surplus": {**_GEN_ACTUAL, "consumption": InputSpec("consumption_actual")},
    "f5_balancing_activation": {
        "direction": InputSpec("balancing.dominating_direction", ("dk.eds.ImbalancePrice.DominatingDirection",)),
        "imbalance_price": InputSpec("imbalance_price.nordic_single", ("dk.eds.ImbalancePrice.ImbalancePriceEUR",)),
        "spot_price": InputSpec("day_ahead_price", ("dk.eds.ImbalancePrice.SpotPriceEUR",)),
    },
}

# F7 reads capacity-map facts (scanner.features.inputs.load_facts), one input per role named in
# scanner.features.substation.ROLES: DSO stations with and without the TSO transformer (the station's
# free MW is the lower of the two), TSO stations in the existing grid.
_DSO_MAP = "dk.kapacitetskort.DSO_stations_capacity_points"
_TSO_MAP = "dk.kapacitetskort.TSO_stations_capacity_points"
FEATURE_INPUTS["f7_substation_saturation"] = {
    "dso_consumption_no_tso": InputSpec("capacity.free_mw.consumption", (f"{_DSO_MAP}.cap_cons_full_no_tso",)),
    "dso_consumption_with_tso": InputSpec("capacity.free_mw.consumption", (f"{_DSO_MAP}.cap_cons_full_with_tso",)),
    "dso_production_no_tso": InputSpec("capacity.free_mw.production", (f"{_DSO_MAP}.cap_prod_full_no_tso",)),
    "dso_production_with_tso": InputSpec("capacity.free_mw.production", (f"{_DSO_MAP}.cap_prod_full_with_tso",)),
    "tso_consumption": InputSpec(
        "capacity.free_mw.consumption", (f"{_TSO_MAP}.available_capacity_consumption_MW.existing_grid",)
    ),
    "tso_production": InputSpec(
        "capacity.free_mw.production", (f"{_TSO_MAP}.available_capacity_production_MW.existing_grid",)
    ),
}


FEATURES: dict[str, FeatureSpec] = {
    f.key: f
    for f in [
        FeatureSpec(
            "f1_price_spread",
            "Price spread",
            "zone",
            ("day_ahead_price",),
            1,
            "f1_price_spread",
            "f1-price-spread-zone",
            outputs=("f1_intraday_spread_native", "f1_intraday_spread_hourly", "f1_interzone_spread"),
            regime_rules=("day_ahead.resolution",),
        ),
        FeatureSpec(
            "f2_curtailment",
            "Curtailment proxy",
            "zone",
            (
                "day_ahead_price",
                "generation_forecast.solar",
                "generation_forecast.wind_onshore",
                "generation_forecast.wind_offshore",
                "generation_actual.solar",
                "generation_actual.wind_onshore",
                "generation_actual.wind_offshore",
            ),
            1,
            "f2_curtailment",
            "f2-curtailment-proxy-zone",
            outputs=("f2_nonpositive_price_share", "f2_curtailment_proxy_mwh"),
            regime_rules=("day_ahead.resolution",),
        ),
        FeatureSpec(
            "f3_interconnector_saturation",
            "Interconnector saturation",
            "zone",
            ("cross_border.physical_flow", "cross_border.transfer_capacity"),
            3,
            "f3_interconnector_saturation",
            "f3-interconnector-saturation-zone-phase-3",
        ),
        FeatureSpec(
            "f4_surplus",
            "Renewable surplus index",
            "zone",
            (
                "generation_actual.solar",
                "generation_actual.wind_onshore",
                "generation_actual.wind_offshore",
                "consumption_actual",
            ),
            1,
            "f4_surplus",
            "f4-renewable-surplus-index-zone",
            outputs=("f4_surplus_index",),
        ),
        FeatureSpec(
            "f5_balancing_activation",
            "Balancing activation",
            "zone",
            ("balancing.dominating_direction", "imbalance_price.nordic_single", "day_ahead_price"),
            1,
            "f5_balancing_activation",
            "f5-balancing-activation-zone",
            outputs=("f5_activation_share_up", "f5_activation_share_down", "f5_imbalance_spot_spread"),
            # docs/features.md: sub-windows split at 2025-03-19 and 2026-03-04.
            regime_rules=("imbalance.price_logic", "imbalance.dominating_direction_basis"),
        ),
        FeatureSpec(
            "f6_renewable_pipeline",
            "Renewable pipeline",
            "municipality",
            (),
            3,
            "f6_renewable_pipeline",
            "f6-renewable-pipeline-municipality-phase-3",
        ),
        FeatureSpec(
            "f7_substation_saturation",
            "Substation saturation",
            "substation",
            (
                "capacity.connection_category.consumption",
                "capacity.connection_category.production",
                "capacity.free_mw.consumption",
                "capacity.free_mw.production",
                "capacity.flexible_connection_possible",
            ),
            1,
            "f7_substation_saturation",
            "f7-substation-saturation-substation",
            outputs=("f7_substation_saturation",),
        ),
        FeatureSpec(
            "f8_queue_pressure",
            "Queue pressure",
            "substation",
            ("connection_queue.applied_mw", "capacity.free_mw.production", "capacity.free_mw.consumption"),
            3,
            None,
            "features-8-to-14-phase-3-spec-before-code",
        ),
    ]
}
