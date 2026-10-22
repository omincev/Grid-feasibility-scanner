"""Valuation assets (docs/valuation.md). Thin wrappers around scanner.valuation.run.

Partitioned by scenario like the scores (scanner.assets.scores): one compute run per scenario partition.
"""

from dagster import (
    AllPartitionMapping,
    AssetDep,
    AssetExecutionContext,
    AssetKey,
    BackfillPolicy,
    MaterializeResult,
    asset,
)

from scanner.assets._todo import NotBuiltYet
from scanner.assets.assumptions import SCENARIO_PARTITIONS, input_keys
from scanner.assets.scores import scenario_deps, scenarios_to_run
from scanner.features.registry import DAY_AHEAD_PRICE
from scanner.runtime.context import repo_state
from scanner.settings import get_settings
from scanner.valuation.run import METHOD, run_bess_v0


@asset(
    key=AssetKey(["valuation", "bess_return_range_v0"]),
    # Every day of the (day-partitioned) price ingests feeds every scenario partition.
    deps=[
        *(AssetDep(k, partition_mapping=AllPartitionMapping()) for k in input_keys(repo_state(), [DAY_AHEAD_PRICE])),
        *scenario_deps(METHOD),
    ],
    partitions_def=SCENARIO_PARTITIONS,
    backfill_policy=BackfillPolicy.single_run(),
    group_name="valuation",
    kinds={"python"},
)
def bess_return_range_v0(context: AssetExecutionContext) -> MaterializeResult:
    """Per scenario and zone (DK1, DK2 and the reference zones): daily LP (scanner.valuation.bess.dispatch_day)
    over day-ahead prices at native resolution, rolling 365-day sums (scanner.valuation.bess.annual_range),
    written to val.run and val.result with metric 'revenue_eur_per_mw_year' and labels ['zone-level',
    'perfect-foresight upper bound', 'day-ahead arbitrage only'], plus 'proposed assumptions (not approved)'
    when the scenario's battery template fell back to proposed values.
    """
    s = get_settings()
    outcomes = [
        run_bess_v0(scenario=sc, database_url=s.analytics_url(), allow_proposed=s.allow_proposed_assumptions)
        for sc in scenarios_to_run(context, METHOD)
    ]
    for o in outcomes:
        for note in o.notes:
            context.log.warning(f"{o.scenario_id}: {note}")
        context.log.info(f"{o.scenario_id}: run {o.run_id}: {o.status}, {o.rows_written} zones")
    return MaterializeResult(
        metadata={
            "runs": "\n".join(f"{o.scenario_id}: {o.run_id} {o.status}" for o in outcomes) or "none",
            "zones_written": sum(o.rows_written for o in outcomes),
            "notes": "\n".join(f"{o.scenario_id}: {n}" for o in outcomes for n in o.notes) or "none",
        }
    )


@asset(key=AssetKey(["valuation", "bess_full"]), group_name="valuation_phase6")
def bess_full(context: AssetExecutionContext) -> None:
    raise NotBuiltYet("WP6.2", "docs/valuation.md#full-bess-model-phase-6")


@asset(key=AssetKey(["valuation", "dsf"]), group_name="valuation_phase6")
def dsf(context: AssetExecutionContext) -> None:
    raise NotBuiltYet("WP6.2", "docs/valuation.md#dsf-model-phase-6")


@asset(key=AssetKey(["valuation", "modular_dc"]), group_name="valuation_phase6")
def modular_dc(context: AssetExecutionContext) -> None:
    raise NotBuiltYet("WP6.2", "docs/valuation.md#modular-dc-model-phase-6-scope-per-open-decision-d3")


VALUATION_ASSETS = [bess_return_range_v0, bess_full, dsf, modular_dc]
