# ADR 0012: Every measure is defined in the codebook; equivalence is claimed and checked

Status: accepted, 2026-10-17.

## Context

"Imbalance price" in Denmark (single price, Nordic pricing module, 15-minute) and in Italy (different settlement, macro-zones) are not the same quantity. "Available capacity" means different things in different capacity maps. A country-agnostic schema makes it easy to put unlike things in one column and produce confident cross-country rankings built on mismatched definitions.

## Decision

Each source field that becomes a series or fact has a codebook entry in `codebook/<cc>/<source>.<dataset>.yaml`. An entry records the source's own definition quoted with a link, our definition, unit, sign convention, settlement or pricing method where relevant, resampling rule, sensitivity, licence, and an `equivalence_class` from `codebook/classes.yaml`. An entry also records who verified it and when.

Assigning an entry to a class is a claim, made by a person (often drafted by a research agent) and signed off by the domain owner. Two checks back the claim: a definitional review, and a numerical reconciliation wherever two sources cover the same zone and class (for example ENTSO-E and Energinet day-ahead prices for DK1 must agree within tolerance).

Features declare the classes they accept in `src/scanner/features/registry.py`. `make contracts` fails if any feature input resolves to a series whose class is not accepted, or if any manifest field lacks a codebook entry.

## Consequences

Cross-country comparisons are explicit decisions. A new source cannot reach a feature until someone has defined and classified what it publishes.
