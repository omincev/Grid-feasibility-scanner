# ADR 0004: Our own entity IDs, with an approved crosswalk

Status: accepted, 2026-10-17.

## Context

The same substation has different names in Energinet's material, each DSO's capacity map, the Danish Energy Agency register and OpenStreetMap. Bidding zones have EIC codes, short codes (DK1) and national names (SUD, Centro-Sud). A wrong match between two sources is the most damaging error an agent can make, because it propagates silently into every score that touches that node.

## Decision

Every entity gets an ID we control, of the form `<cc>.<kind>.<slug>`, for example `dk.zone.dk1` or `dk.substation.energinet.idomlund`. Kinds: `country`, `zone`, `tso_area`, `dso_area`, `substation`, `transformer`, `line`, `municipality`, `plant`, `site`. Entities form a hierarchy through `parent_id`. Bidding zones also store their EIC code.

Source-specific names and IDs live in `ref.entity_alias`, one row per source naming. Aliases are curated in `registry/entities/<cc>/` and loaded from git. Agents may propose matches with evidence (distance, name similarity, voltage, capacity); a human approves every match. Unmatched source entities are stored with their source ID and flagged, never force-matched.

## Consequences

Matching is slow and human-gated, which is intended. Every join across sources goes through the alias table, so a wrong match can be fixed in one place and every dependent asset recomputes.
