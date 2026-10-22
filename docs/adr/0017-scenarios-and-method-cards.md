# ADR 0017: Methods and analytics assumptions are explicit, versioned and testable as scenarios

Status: accepted, 2026-10-18, owner decision.

## Context

Field experts must be able to ask how any number was computed, and to test competing hypotheses (for example how substation saturation enters a score) without code changes. Several method choices had ended up in Python constants: which statistic feeds the score, the percentile population, the missing-feature policy, the municipality rule, the assumption set names.

## Decision

Every calculation has a method card in `docs/methods/` (formula in words, inputs, parameters, outputs, known biases, owner, version, status); code declares the method it implements and every output records its method ID and version. Method choices live in assumption sets, never in code. A scenario (`assumptions/scenarios/<id>.yaml`) names the assumption sets it uses plus method switches and overrides; exactly one scenario is official, the others exploratory. Scenarios carry analytics assumptions only; ingestion and normalisation rules stay in the codebook, manifests and dossiers. Features are scenario-independent. Every score, opportunity and valuation row records its scenario and the content hashes of the scenario and its sets. Serving views show the official scenario's latest complete run; `serving.scenario_comparison`, `serving.assumption_register` and `serving.method_catalog`, and the generated `exports/methods_and_scenarios.html`, show the rest. `make contracts` fails on a method without a card or a parameter that does not resolve.

## Consequences

A new hypothesis is a new scenario file; choosing the official one is a signed-off change (H1, H7). Past results stay reproducible because nothing is overwritten. Each scenario adds a run of scores and valuation to the daily job.
