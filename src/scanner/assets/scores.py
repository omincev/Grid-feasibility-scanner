"""Scores and opportunity types (docs/features.md, docs/methods/). Thin wrappers around scanner.scores.run.

Partitioned by scenario (scanner.assets.assumptions.SCENARIO_PARTITIONS): one compute run per scenario
partition. A run over several partitions (the daily job covers them all) computes the official scenario first.
Each asset depends on the features it reads, the scenario files, the assumption sets any scenario names for
the roles its methods read, its method cards and the curated load. Runs connect with ANALYTICS_DATABASE_URL
(default DATABASE_URL).
"""

from collections.abc import Callable, Sequence

import psycopg
from dagster import (
    AssetExecutionContext,
    AssetKey,
    BackfillPolicy,
    Failure,
    MaterializeResult,
    asset,
)

from scanner import REPO_ROOT
from scanner.assets._todo import NotBuiltYet
from scanner.assets.assumptions import (
    CURATED_LOAD_KEY,
    SCENARIO_PARTITIONS,
    SCENARIOS_KEY,
    method_keys,
    roles,
    set_keys,
    stale_copies,
)
from scanner.assets.features import MVP_FEATURE_ASSETS
from scanner.core.methods import MethodRef
from scanner.core.registry import load_scenarios
from scanner.runtime.scenarios import scenario_ids
from scanner.scores.run import (
    OPPORTUNITY_METHOD,
    SITE_METHOD,
    ZONE_METHOD,
    ScoreOutcome,
    run_congestion_scores,
    run_opportunity_types,
)
from scanner.settings import get_settings

CONGESTION_KEY = AssetKey(["scores", "congestion"])
OPPORTUNITY_KEY = AssetKey(["scores", "opportunity_types"])


def scenario_deps(*methods: MethodRef) -> list[AssetKey]:
    """The scenario files, the sets the methods read in any scenario, the method cards and the curated load."""
    return [
        SCENARIOS_KEY,
        *set_keys(load_scenarios(REPO_ROOT).values(), *methods),
        *method_keys(*methods),
        CURATED_LOAD_KEY,
    ]


def scenarios_to_run(context: AssetExecutionContext, *methods: MethodRef) -> list[str]:
    """The run's scenario partitions, the official one first. Fails while ref holds an older copy of a
    scenario file or of a set it uses than the files: launch job_compute, which loads them first."""
    with psycopg.connect(get_settings().analytics_url()) as conn:
        order = scenario_ids(conn)
        stale = {s: stale_copies(conn, REPO_ROOT, s, roles(*methods)) for s in context.partition_keys}
    if problems := {s: files for s, files in stale.items() if files}:
        raise Failure(
            "ref holds older copies of the files these scenarios use: "
            + "; ".join(f"{s}: {', '.join(files)}" for s, files in problems.items())
            + ". Run job_compute (it loads the curated files first) or materialize curated/analytics_load."
        )
    return sorted(context.partition_keys, key=lambda s: order.index(s) if s in order else len(order))


def per_scenario(
    context: AssetExecutionContext, methods: Sequence[MethodRef], run: Callable[[str], ScoreOutcome]
) -> MaterializeResult:
    outcomes = [run(s) for s in scenarios_to_run(context, *methods)]
    for o in outcomes:
        for note in o.notes:
            context.log.warning(f"{o.scenario_id}: {note}")
        context.log.info(f"{o.scenario_id}: run {o.run_id}: {o.status}, {o.rows_written} rows")
    return MaterializeResult(
        metadata={
            "runs": "\n".join(f"{o.scenario_id}: {o.run_id} {o.status}" for o in outcomes) or "none",
            "rows_written": sum(o.rows_written for o in outcomes),
            "notes": "\n".join(f"{o.scenario_id}: {n}" for o in outcomes for n in o.notes) or "none",
        }
    )


@asset(
    key=CONGESTION_KEY,
    deps=[*(a.key for a in MVP_FEATURE_ASSETS), *scenario_deps(ZONE_METHOD, SITE_METHOD)],
    partitions_def=SCENARIO_PARTITIONS,
    backfill_policy=BackfillPolicy.single_run(),
    group_name="scores",
    kinds={"python"},
)
def congestion_score(context: AssetExecutionContext) -> MaterializeResult:
    """Per scenario: percentile per input, weighted sum with the scenario's weights and method choices, full
    breakdown stored, for active-country zones and their municipalities. Refuses to run while required
    assumption values are null and proposed values are not allowed (MissingAssumption names the owners)."""
    s = get_settings()
    return per_scenario(
        context,
        (ZONE_METHOD, SITE_METHOD),
        lambda sc: run_congestion_scores(
            scenario=sc, database_url=s.analytics_url(), allow_proposed=s.allow_proposed_assumptions
        ),
    )


@asset(
    key=OPPORTUNITY_KEY,
    deps=[CONGESTION_KEY, *scenario_deps(OPPORTUNITY_METHOD)],
    partitions_def=SCENARIO_PARTITIONS,
    backfill_policy=BackfillPolicy.single_run(),
    group_name="scores",
    kinds={"python"},
)
def opportunity_types(context: AssetExecutionContext) -> MaterializeResult:
    """Per scenario: its opportunity rules on its latest congestion run, into feat.opportunity."""
    s = get_settings()
    return per_scenario(
        context,
        (OPPORTUNITY_METHOD,),
        lambda sc: run_opportunity_types(
            scenario=sc, database_url=s.analytics_url(), allow_proposed=s.allow_proposed_assumptions
        ),
    )


@asset(key=AssetKey(["scores", "grid_friendliness"]), group_name="scores_phase3")
def grid_friendliness_score(context: AssetExecutionContext) -> None:
    raise NotBuiltYet("WP3.10", "docs/features.md#grid-friendliness-score-phase-3-open-decision-d4")


SCORE_ASSETS = [congestion_score, opportunity_types, grid_friendliness_score]
