"""Forecasts (WP6.7, docs/quality.md layer 5).

Models read covariates as of issue time (core.observation_asof), write quantile forecasts back as
series of kind 'forecast' with issued_at and model ID, and are served only while they beat the naive
and seasonal-naive baselines on the rolling backtest.
"""

from dagster import AssetExecutionContext, AssetKey, asset

from scanner.assets._todo import NotBuiltYet


@asset(key=AssetKey(["forecasts", "baselines"]), group_name="forecasts_phase6")
def baselines(context: AssetExecutionContext) -> None:
    """Naive (tomorrow equals today) and seasonal-naive (same day last week) for day-ahead price and spread."""
    raise NotBuiltYet("WP6.7", "docs/quality.md#5-forecast-gate")


@asset(
    key=AssetKey(["forecasts", "timestral_day_ahead"]),
    deps=[AssetKey(["forecasts", "baselines"])],
    group_name="forecasts_phase6",
)
def timestral_day_ahead(context: AssetExecutionContext) -> None:
    """Timestral quantile forecast of next-day prices and spread with past and known-future covariates."""
    raise NotBuiltYet("WP6.7", "docs/quality.md#5-forecast-gate")


FORECAST_ASSETS = [baselines, timestral_day_ahead]
