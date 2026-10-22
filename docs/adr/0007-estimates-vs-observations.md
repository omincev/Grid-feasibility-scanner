# ADR 0007: Estimates are their own record type

Status: accepted, 2026-10-17.

## Context

Many values the scanner needs are published nowhere: node saturation in Italy and Hungary, curtailment per area, network topology. They can only be inferred from proxies. If inferred values sit in the same column as measured ones, nobody downstream can tell them apart.

## Decision

Measured values are observations or facts. Inferred values are rows in `core.estimate` with `low`, `mid`, `high`, `method`, `method_version`, the IDs of every input series, fact and claim, and the run ID. Where possible the same quantity is estimated from two or more independent proxies, and each estimate is stored separately with its own method. A serving view may combine them, but must expose the spread. Methods live in `src/scanner/assets/estimates.py` with a spec in `docs/features.md`, and method changes need sign-off from the country's domain owner.

The UI shows estimated values with a different visual treatment and a "how we estimated this" panel fed by the stored inputs.

## Consequences

Scores and valuations inherit ranges from their inputs instead of false precision. The estimates layer becomes the place where proprietary data later replaces proxies, one estimate at a time.
