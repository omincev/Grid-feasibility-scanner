"""Estimates for values no source publishes (ADR 0007, Phase 5). Each method gets a spec section first."""

from dagster import AssetExecutionContext, AssetKey, asset

from scanner.assets._todo import NotBuiltYet


@asset(key=AssetKey(["estimates", "node_saturation"]), group_name="estimates_phase5")
def node_saturation(context: AssetExecutionContext) -> None:
    """Node saturation for IT and HU from independent proxies; low/mid/high per method in core.estimate."""
    raise NotBuiltYet("WP5.5", "docs/features.md#estimates-phase-5")


@asset(key=AssetKey(["estimates", "curtailment_by_area"]), group_name="estimates_phase5")
def curtailment_by_area(context: AssetExecutionContext) -> None:
    raise NotBuiltYet("WP5.5", "docs/features.md#estimates-phase-5")


ESTIMATE_ASSETS = [node_saturation, curtailment_by_area]
