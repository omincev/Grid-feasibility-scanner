# Scenarios

A scenario is one complete, named set of analytics assumptions: which assumption set each calculation uses, and which of their values it replaces. Experts compare scenarios side by side (`serving.scenario_comparison`, `exports/methods_and_scenarios.html`). Scenarios carry analytics assumptions only (scores, opportunity rules, valuation); ingestion and normalisation rules stay in the codebook, manifests and dossiers as reviewed changes. Schema: `scanner.core.registry.Scenario`.

```yaml
id: demo_default                  # file name without .yaml
title: Demo default
status: official                  # official | exploratory; exactly one scenario is official
description: >-                   # what this scenario assumes and why, in plain words
  ...
owners: [oleg, michael]
approved_by: null                 # set both to approve the scenario's own values
approved_on: null
sensitivity: public
sets:                             # role -> assumption set ID (assumptions/<set>.yaml)
  score_weights: score_weights_v0
  score_method: score_method_v0
  opportunity_rules: opportunity_rules_v0
  bess_template: bess_mvp
method_switches:                  # categorical method choices, "<role>.<parameter>": value
  score_method.municipality_missing_input_policy: null_component
overrides: {}                     # other replaced values (hypotheses), "<role>.<parameter>": value
```

Roles are the names the code resolves parameters by; each method card (`docs/methods/`) lists its parameters as `<role>.<parameter>`. A switch or override replaces that parameter of the role's set for this scenario only. It counts as an approved value when the scenario has `approved_by` and `approved_on`, otherwise as a proposed one: outputs then need `SCANNER_ALLOW_PROPOSED_ASSUMPTIONS=true` and carry the label "proposed assumptions (not approved)", like any proposed value.

Every score, opportunity and valuation row records `scenario_id`, the scenario's content hash, the content hashes of its sets and the method versions. Features are scenario-independent: a scenario that needs another statistic gets it from the one feature run everyone uses. `make contracts` checks that exactly one scenario is official, that every set and every switched or overridden parameter exists, and that the method choices are ones the code implements.

Run: `uv run python -m scanner.cli pipeline --all-scenarios` (or `--scenario <id>`, repeatable; default the official one). Run `load-curated` after editing a scenario (the pipeline does it first).

Saturation and market scenarios for the valuation engine (WP6.4) will be assumption sets with their own role, named by a scenario like any other set.
