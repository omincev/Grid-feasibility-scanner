"""Serving exports (WP1.11). Views themselves are defined in db/migrations."""

from dagster import AssetExecutionContext, AssetKey, BackfillPolicy, MaterializeResult, asset

from scanner.assets.assumptions import CURATED_LOAD_KEY
from scanner.assets.scores import CONGESTION_KEY, OPPORTUNITY_KEY
from scanner.assets.valuation import bess_return_range_v0
from scanner.runtime.export import export_static
from scanner.runtime.report import write_report
from scanner.settings import get_settings


@asset(
    key=AssetKey(["serving", "static_export"]),
    # Every scenario partition of the scores and valuation; the report also shows the loaded cards and sets.
    deps=[CONGESTION_KEY, OPPORTUNITY_KEY, bess_return_range_v0.key, CURATED_LOAD_KEY],
    group_name="serving",
    kinds={"python"},
    backfill_policy=BackfillPolicy.single_run(),
    description="Writes every serving view to exports/ as JSON or GeoJSON (fallback for the demo), and the "
    "methods and scenarios report (exports/methods_and_scenarios.html and .md).",
)
def static_export(context: AssetExecutionContext) -> MaterializeResult:
    url = get_settings().analytics_url()
    written = [*export_static(database_url=url), *write_report(database_url=url)]
    return MaterializeResult(metadata={"files": "\n".join(written)})


SERVING_ASSETS = [static_export]
