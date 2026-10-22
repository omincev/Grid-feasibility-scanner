---
id: opportunity_rules
version: 1
title: Opportunity types
status: provisional
owner: [oleg, michael]
inputs: [feat.score breakdown (percentiles per weight key), f7_substation_saturation (area shares)]
parameters: [opportunity_rules.high_percentile, opportunity_rules.rules]
outputs: [feat.opportunity, serving.opportunities, zone_card.asset_types, ranked_sites.asset_types]
known_biases:
  - Zones and municipalities are areas, so substation conditions are existence tests on area shares, not joint conditions at one station.
  - Industrial load nearby has no source yet, so demand-side flexibility is at best a candidate that needs site data.
  - A "high" percentile is relative to the score's population, so a type can appear or vanish when countries are added.
---

Specification: `docs/features.md`, "Opportunity type rules". Code: `scanner.scores.opportunity` (rules), `scanner.scores.run.run_opportunity_types`. Rules and threshold come from the scenario's `opportunity_rules` set.

**What it decides.** For each scored zone and municipality and each asset type (battery, demand-side flexibility, modular data centre), whether the area matches, is a candidate that needs site data, or does not match.

**Conditions** (`opportunity_rules.rules`, threshold `opportunity_rules.high_percentile`).

- `high: [<weight key>]`: that input's percentile in the score breakdown is at least the threshold (75 proposed). Unknown when the score has no value for it.
- `requires: [industrial_load_nearby]`: always unknown for now (no source).
- `substation_not_saturated: production_or_consumption`: in at least one direction the area has a station with free capacity (share of stations without free capacity below 1).
- `substation: {production: saturated, consumption: not_saturated}`: at least one station without free production capacity (share above 0) and at least one with free consumption capacity.

Proposed rules: battery = high F1 and high F5 and a direction not saturated; demand-side flexibility = high F5 and industrial load nearby; modular data centre = high F2 and high F4, production saturated and consumption not.

**Status.** `no_match` if a condition failed or a market percentile is unknown; `match` if all passed; `candidate_needs_site_data` if nothing failed and only site data is unknown. Each condition is stored with its value, operator, threshold and result.
