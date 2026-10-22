# Assumption sets

Every parameter that is a judgement (weights, thresholds, asset specifications, costs, financing) lives here as a versioned set, never as a constant in code. Schema: `scanner.core.registry.AssumptionSet`.

Each parameter has a `value` (null until approved) and optionally a `proposed` value with its origin. Assets refuse to run with a null required value and report which parameter and owner are missing. With `SCANNER_ALLOW_PROPOSED_ASSUMPTIONS=true` they fall back to `proposed` instead (`AssumptionSet.resolve`, `scanner.runtime.assumptions`); every output then records which parameters came from proposed values and carries the label "proposed assumptions (not approved)". Outputs always record the set ID and the `content_hash` of the loaded set, so run `load-curated` after editing a set. A set's `status` is `proposed` until an owner sets `approved_by` and `approved_on`, then `approved`. Approved sets are never edited; a change creates `_v1`, `_v2` and so on, so past results stay reproducible.

Method choices are parameters too: `score_method_v0` holds which feature statistic feeds which score and in which direction, the percentile populations, the missing-input policy and the municipality inheritance rule. Code never names a set: a scenario (`scenarios/`, see its README) names the set for each role and may switch or override parameter values; runs record the scenario and the hashes of the scenario and its sets. Method cards (`docs/methods/`) list the `<role>.<parameter>` names each calculation uses.

Sensitive sets (private PPA prices, customer parameters) do not go here; they go in the restricted repository (ADR 0006).
