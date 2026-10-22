"""Feature assets (docs/features.md). Each writes feat.feature_value with run ID and code version.

The assets are thin wrappers around scanner.features.run.run_features, which loads inputs
(scanner.features.inputs), computes (scanner.features.zone) and records one ops.compute_run per feature.
Features are scenario-independent, so they are not partitioned. Each depends on what it reads: the ingest
or capacity-map assets of its inputs' codebook entries (scanner.features.registry.FEATURE_INPUTS), its method
card, and the curated load. They connect with ANALYTICS_DATABASE_URL (default DATABASE_URL).

Rules for implementers:
- read only series whose codebook entry class is in the feature's accepted_classes
  (scanner.features.registry); read via core.observation_effective, rows with usable = true (quality ok,
  or quarantined and not rejected by a person; scanner.runtime.quarantine) unless the spec says otherwise,
  and record how many unverified values were used (scanner.features.inputs.review_counts)
- resample only with scanner.core.time.resample and the codebook rule; daily statistics use local days
- sensitivity of each output row = scanner.core.sensitivity.combine over the inputs
- write summaries for the whole window and for each regime sub-window listed in the spec
"""

from dagster import AssetExecutionContext, AssetKey, BackfillPolicy, MaterializeResult, asset

from scanner.assets._todo import NotBuiltYet
from scanner.assets.assumptions import CURATED_LOAD_KEY, input_keys, method_keys
from scanner.features.registry import FEATURE_INPUTS
from scanner.features.run import METHODS, run_features
from scanner.runtime.context import repo_state
from scanner.settings import get_settings


def feature_deps(feature: str) -> list[AssetKey]:
    """The assets a feature reads, its method card, and the curated load."""
    return [
        *input_keys(repo_state(), FEATURE_INPUTS[feature].values()),
        *method_keys(METHODS[feature]),
        CURATED_LOAD_KEY,
    ]


def _materialize(context: AssetExecutionContext, feature: str) -> MaterializeResult:
    (outcome,) = run_features([feature], database_url=get_settings().analytics_url())
    for note in outcome.notes:
        context.log.warning(note)
    context.log.info(f"run {outcome.run_id}: {outcome.status}, {outcome.rows_written} rows")
    return MaterializeResult(
        metadata={
            "run_id": outcome.run_id,
            "status": outcome.status,
            "rows_written": outcome.rows_written,
            "notes": "\n".join(outcome.notes) or "none",
        }
    )


def _feature(key: str, description: str):  # type: ignore[no-untyped-def]
    return asset(
        key=AssetKey(["features", key]),
        deps=feature_deps(key),
        group_name="features",
        kinds={"python"},
        description=description,
        backfill_policy=BackfillPolicy.single_run(),
    )


@_feature("f1_price_spread", "F1: intraday spread (native and hourly) and DK1-DK2 inter-zone spread.")
def f1_price_spread(context: AssetExecutionContext) -> MaterializeResult:
    return _materialize(context, "f1_price_spread")


@_feature(
    "f2_curtailment",
    "F2: non-positive price share and curtailment proxy (day-ahead forecast minus actual production).",
)
def f2_curtailment(context: AssetExecutionContext) -> MaterializeResult:
    return _materialize(context, "f2_curtailment")


@_feature("f4_surplus", "F4: renewable surplus index from production and consumption actuals.")
def f4_surplus(context: AssetExecutionContext) -> MaterializeResult:
    return _materialize(context, "f4_surplus")


@_feature(
    "f5_balancing_activation",
    "F5: activation shares and imbalance-spot spread, split at the 2025-03-19 and 2026-03-04 breaks.",
)
def f5_balancing_activation(context: AssetExecutionContext) -> MaterializeResult:
    return _materialize(context, "f5_balancing_activation")


@_feature(
    "f7_substation_saturation",
    "F7: free MW per substation and direction, rank in zone, and zone and municipality shares "
    "(scanner.features.substation). The source publishes MW; never a derived % or category.",
)
def f7_substation_saturation(context: AssetExecutionContext) -> MaterializeResult:
    return _materialize(context, "f7_substation_saturation")


@asset(key=AssetKey(["features", "f3_interconnector_saturation"]), group_name="features_phase3", kinds={"python"})
def f3_interconnector_saturation(context: AssetExecutionContext) -> None:
    raise NotBuiltYet("WP3.4", "docs/features.md#f3-interconnector-saturation-zone-phase-3")


@asset(key=AssetKey(["features", "f6_renewable_pipeline"]), group_name="features_phase3", kinds={"python"})
def f6_renewable_pipeline(context: AssetExecutionContext) -> None:
    raise NotBuiltYet("WP3.4", "docs/features.md#f6-renewable-pipeline-municipality-phase-3")


MVP_FEATURE_ASSETS = [f1_price_spread, f2_curtailment, f4_surplus, f5_balancing_activation, f7_substation_saturation]
FEATURE_ASSETS = [*MVP_FEATURE_ASSETS, f3_interconnector_saturation, f6_renewable_pipeline]
