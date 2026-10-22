---
id: congestion_score
version: 1
title: Congestion score (zones)
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
  - score_method.zone_inputs
  - score_method.zone_missing_input_policy
  - score_method.day_based_population
  - score_method.area_population
outputs: [feat.score (level zone), serving.zone_card, serving.zone_feature_breakdown]
known_biases:
  - A zone's window statistic is ranked against single days, which spread wider than window means, so zone percentiles bunch towards the middle.
  - The pooled population mixes every zone with data (DK1, DK2, DE-LU, NL, NO2, SE3, SE4 for prices), so it depends on which countries are configured.
  - With two Danish zones, an area input such as F7 can only take the zone percentiles 25 and 75 (open decision D1).
  - Weights and method choices are proposed values until the owners approve them.
---

Specification: `docs/features.md`, "Congestion score". Code: `scanner.scores.run` (zone part of `country_scores`), `scanner.scores.congestion` (maths), `scanner.scores.method` (method choices). Every value it uses comes from the scenario (`assumptions/scenarios/`).

**What it measures.** Where a zone stands, from 0 to 100, on the market signals that make a flexible asset valuable: spreads, low prices, renewable surplus, balancing, and (if the scenario includes it) grid saturation.

**Inputs.** The weight keys listed in `score_method.zone_inputs` that have a weight in `score_weights.weights`. For each, `score_method.inputs` names the feature statistic used as the zone's raw value (its value over the whole analysis window, from the latest feature run) and whether a higher value means more opportunity. In `spec_v0` the zone inputs are F1, F2, F4, F5 and F7; in `demo_default` F7 is left out of zones.

**Percentile per input** (`score_method.day_based_population`, `score_method.area_population`).

- Day-based inputs (`pooled_daily_values`): the zone's window statistic is ranked within the per-local-day values of the same statistic of every zone in that feature run, pooled over the window.
- Area inputs (`peer_areas`, F7): ranked among the zones that have the statistic.
- Percentile = 100 × (values below + half the values equal) ÷ population size; ties share a percentile. Where a lower value means more opportunity, 100 minus that.

**Weights and score** (`score_method.zone_missing_input_policy`).

- `renormalise` (both scenarios for zones): the weights of the inputs the zone has are scaled to sum to 100.
- `null_component`: the weights of all zone inputs are scaled to sum to 100; an input without data stays in the breakdown with a null value and a flag, and adds nothing.
- Contribution = weight × percentile ÷ 100; the score is the sum of the contributions. Weights in the set without an input (F3, F6 until Phase 3) never count and are listed as `missing_features`.

The breakdown (`feat.score.breakdown`) stores, per input, raw value, percentile, weight, contribution, population, feature run and period, plus the scenario, the sets with their hashes and which values were proposed.
