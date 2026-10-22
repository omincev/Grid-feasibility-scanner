# ADR 0011: Countries are configuration; market rules are dated data

Status: accepted, 2026-10-17.

## Context

The scanner must cover Denmark, Italy and Hungary, and add further countries quickly. Market design differs between countries and changes over time inside one country (15-minute day-ahead from 2025-10-01, Danish imbalance pricing changes in 2025 and 2026). Code full of `if country == "DK"` breaks both goals.

## Decision

Two tiers of sources. The base tier is ENTSO-E, which publishes the same document types for every European bidding zone; adding a country covered by it is a new `registry/countries/<cc>.yaml` and nothing else. The enrichment tier is national sources (TSO and DSO portals, registers, regulators), each with its own adapter built from an approved dossier.

Each country file declares bidding zones with EIC codes, IANA timezone, currency, the geography hierarchy levels that exist, and per-measure source precedence. Market design lives in `market_rules/<cc>.yaml` as dated entries (settlement period, imbalance pricing method, reserve products, capacity mechanisms, connection regimes, support schemes), loaded into `ref.market_rule` with `valid_from` and `valid_to`.

Feature, estimate and valuation code reads rules from `ref.market_rule` for the zone and date in question. Code paths may branch on rule values, never on country codes.

`tests/test_agnostic.py` adds the Netherlands from `registry/countries/nl.yaml` and requires a score with no code changes. It stays in CI permanently.

## Consequences

New countries are cheap at the base tier. Rule changes inside a country are data edits with sign-off, not code changes. Every feature must be written against rules, which is slower at first.
