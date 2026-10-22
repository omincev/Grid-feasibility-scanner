---
id: municipality_site_score
version: 2
title: Municipality site score
status: provisional
owner: [oleg, michael]
inputs:
  - f1_intraday_spread_hourly
  - f2_nonpositive_price_share
  - f4_surplus_index
  - f5_imbalance_spot_spread
  - f7_substation_saturation
parameters:
  - score_weights.weights
  - score_method.inputs
  - score_method.municipality_inputs
  - score_method.municipality_missing_input_policy
  - score_method.municipality_inheritance
  - score_method.day_based_population
  - score_method.area_population
  - score_method.municipality_grid_direction
  - score_method.municipality_direction_inputs
  - score_method.asset_grid_directions
  - score_method.municipality_min_stations
outputs: [feat.score (level municipality), serving.ranked_sites]
known_biases:
  - All municipalities of a zone share the zone's market percentiles; only F7 separates them, so many sites tie.
  - The station-count statistics weigh a large and a small station the same; the MW statistics (f7_mw_weighted) rest on values the publisher rounds to 10 or 20 MW steps and that the source says cannot be added up.
  - A municipality with few stations takes extreme F7 values (with one station a share is 0 or 1); it is flagged low_station_count, not dropped.
  - Under renormalise, a municipality without stations is ranked as if F7 did not exist; under null_component it gets no F7 points, which favours municipalities whose stations were measured.
  - The score uses zone-level market data; nothing about the site itself (land, connection cost, distance to the station) enters.
  - With municipality_grid_direction any, the F7 input is the same for every asset type; the default (share without free production capacity) also rewards stations where a load cannot connect either.
---

Specification: `docs/features.md`, "Congestion score" (municipality site scores). Code: `scanner.scores.run` (municipality part of `country_scores`), `scanner.scores.method` (method choices). Every value it uses comes from the scenario.

**What it measures.** A site card's score for each municipality of a scored zone, on the same 0 to 100 scale as the zone score, for the asset types of the scenario's grid direction.

**Inputs.** The weight keys in `score_method.municipality_inputs` that have a weight (in every scenario F1, F2, F4, F5 and F7).

- Day-based inputs (`score_method.municipality_inheritance` = `parent_zone_percentiles`): the municipality takes its parent zone's percentile unchanged, computed as for the zone score, also when the zone score itself leaves that input out.
- Area inputs (F7, `score_method.area_population` = `peer_areas`): the municipality's own F7 statistic, ranked among the country's municipalities that have it. Which statistic depends on the grid direction.

**Grid direction** (`score_method.municipality_grid_direction`, `score_method.asset_grid_directions`, `score_method.municipality_direction_inputs`). Asset types are load-type (they draw power where they create value: modular DC, demand-side flexibility, BESS at its charging side) or production-type (they feed in; none is an asset type yet).

- `any` (`spec_v0`, `demo_default`): the F7 statistic of `score_method.inputs`, the share of DSO stations without free production capacity; the same for every asset type.
- `load` (`f7_direction_aware`, `f7_mw_weighted`): the entry `load` of `municipality_direction_inputs`. Proposed: the share of the municipality's DSO stations, among those assessed in both directions, with free consumption capacity and no free production capacity (`share_stations_free_consumption_no_free_production`); `f7_mw_weighted` uses the free consumption MW at those stations per assessed station (`free_mw_consumption_mean_no_free_production`).
- `production`: the entry `production`, the reverse statistics (`share_stations_free_production_no_free_consumption`, `free_mw_production_mean_no_free_consumption`).

The breakdown records the direction and the asset types the score serves under `site`.

**Small samples** (`score_method.municipality_min_stations`, proposed 3, a modelling convention). The count the F7 statistic rests on (for example `stations_assessed_both` for the direction-aware shares, `stations_assessed_production` for the share without free production capacity) is recorded under `station_sample`. Below the minimum the municipality gets the flag `low_station_count`; it stays in the percentile population and is ranked like any other.

**Missing station data** (`score_method.municipality_missing_input_policy`). A municipality without a value of the F7 statistic (no capacity-map stations, or none assessed in the directions it needs) has no F7 component.

- `renormalise` (`spec_v0`): it is scored on the inherited inputs alone, their weights scaled to sum to 100, so it ranks level with its zone's other municipalities without stations.
- `null_component` (`demo_default` and the F7 scenarios): F7 stays in the breakdown with a null value and the flag `no_station_data`; the weights are scaled over all municipality inputs, and the score is the sum of the known contributions, so the F7 share of the weights (25 of 100 with the v0 weights) is not earned.

Contribution = weight × percentile ÷ 100; the score is their sum. The breakdown records `parent_zone`, `inherited_from` per inherited input, `flags`, `missing_inputs`, `site` and `station_sample`.
