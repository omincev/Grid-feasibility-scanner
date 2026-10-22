# Features, scores and opportunity types

Specifications that feature assets must implement exactly. A change to a definition here needs sign-off from Oleg and Michael (H7). Each feature declares its level (zone, substation, municipality), its accepted codebook equivalence classes in `src/scanner/features/registry.py`, and its target resolution. Every feature value is written with run ID, code version and sensitivity.

The analysis window for the MVP is 2025-03-04 00:00 Europe/Copenhagen to the latest complete local day. 2025-03-04 is the start of the `ImbalancePrice` dataset. Regime breaks inside the window (see `market_rules/dk.yaml`) are reported per sub-window as well as for the whole window.

## Features 1 to 7 (MVP: 1, 2, 4, 5, 7)

### F1 Price spread (zone)

Inputs: day-ahead price, class `day_ahead_price`. `Elspotprices` before 2025-10-01 (PT1H), `DayAheadPrices` from 2025-10-01 (PT15M).

Outputs, each its own feature key:

- `f1_intraday_spread_native`: per local day, max minus min of the day-ahead price at native resolution. Summary: mean and P90 over the window.
- `f1_intraday_spread_hourly`: the same after resampling to PT1H with `mean`. This is the comparable series across the resolution switch.
- `f1_interzone_spread`: per interval, absolute DK1 minus DK2 price at native resolution. Summary: mean, and share of intervals above 10 EUR/MWh.

### F2 Curtailment proxy (zone)

Inputs: day-ahead price; wind and solar day-ahead forecast (`Forecasts_Hour.ForecastDayAhead`); actual wind and solar production (dataset to be chosen in WP1.3).

- `f2_nonpositive_price_share`: share of intervals with day-ahead price at or below 0.
- `f2_curtailment_proxy_mwh`: over intervals with price at or below 0, the sum of max(0, day-ahead forecast minus actual) for wind plus solar, in MWh. This is a proxy: forecast error has other causes. Label it so in the UI.

### F3 Interconnector saturation (zone, Phase 3)

Inputs: ENTSO-E physical flows and transfer capacity per border. `f3_saturation_share`: per border, share of hours with flow at or above 95% of capacity in the export direction; zone value is the capacity-weighted mean over its borders.

### F4 Renewable surplus index (zone)

Inputs: actual wind and solar production, actual consumption (WP1.3). `f4_surplus_index`: per interval, (wind + solar minus consumption) divided by consumption. Summary: mean and share of intervals above 0.

### F5 Balancing activation (zone)

Inputs: `ImbalancePrice.DominatingDirection`, `ImbalancePrice.ImbalancePriceEUR`, `ImbalancePrice.SpotPriceEUR`.

- `f5_activation_share_up`, `f5_activation_share_down`: share of intervals with dominating direction 1 and minus 1.
- `f5_imbalance_spot_spread`: mean absolute difference between imbalance price and spot price over intervals where both are non-null.

Report for sub-windows split at 2025-03-19 and 2026-03-04 (imbalance price logic change; dominating direction basis change).

### F6 Renewable pipeline (municipality, Phase 3)

Inputs: Danish Energy Agency master data register; planned projects where a source exists. `f6_running_mw`, `f6_planned_mw` per municipality, by technology.

### F7 Substation saturation (substation)

Inputs: DSO capacity maps (BEK 1468/2025 §29) and Energinet transmission capacity information, as facts.

Store what the source gives, in this order of preference: used percentage if the source publishes both used and total capacity; free MW if it publishes only free MW; category (free, limited, none) if it publishes only categories. Never convert free MW or a category into a percentage. Separate values for the consumption and production directions. Also store the flexible-connection indicator where published.

For scoring, F7 is converted to a rank within its zone using the best available representation per substation, with ties for equal categories. The UI colours substations green, yellow or red using `assumptions/saturation_thresholds_v0.yaml` for percentages and the category itself otherwise.

## Features 8 to 14 (Phase 3, spec before code)

Each needs a section here, signed off, before its bead is released: F8 queue pressure (MW requested vs free MW per substation, from Energinet's queue map and capacity maps), F9 grid-friendliness inputs (flexible connection possible, shared connection point possible, direction of local constraint relative to the asset's flow), F10 solar dip (depth of the midday price drop relative to the daily mean), F11 ancillary capacity prices (FCR, aFRR, mFRR capacity per zone), F12 industrial load nearby (Layer 2 and public industrial registers), F13 PPA potential (curtailed wind or solar within X km), F14 fibre and district-heating distance.

## Congestion score

For each feature summary used in the score, compute a percentile from 0 to 100, then a weighted sum with weights from `assumptions/score_weights_v0.yaml`. Store raw value, percentile, weight and contribution per feature in `feat.score.breakdown`.

Open decision D1 covers the reference population for percentiles. With only DK1 and DK2, a percentile across zones is degenerate (0 or 100). Default until D1 is decided: compute each zone's percentile against the pooled distribution of daily feature values of all zones available for that feature in the window. For price-based features this includes DE, NO2, SE3 and SE4, which `DayAheadPrices` also publishes. Substations are ranked within their zone. Once ENTSO-E is in (Phase 3), the population becomes all bidding zones in the reference set defined in the assumption file.

> **MVP provisional, pending H7** (implemented in `scanner.scores`, WP1.8 and WP1.12). Every choice below is a parameter of an assumption set named by a scenario (`assumptions/scenarios/`, `assumptions/score_method_v0.yaml`); the method cards `docs/methods/congestion_score.md` and `municipality_site_score.md` describe them, and each score row records its scenario, set hashes and method versions. The bullets describe scenario `spec_v0`; the official scenario `demo_default` differs in two method switches (F7 out of the zone score; a municipality without stations gets a null F7 flagged `no_station_data` instead of renormalised weights).
>
> - Inputs are the `inputs` parameter of the scenario's `score_method` set: (`f1_intraday_spread_hourly`, `mean`), (`f2_nonpositive_price_share`, `share`), (`f4_surplus_index`, `mean`), (`f5_imbalance_spot_spread`, `mean`) and F7 as (`f7_substation_saturation`, `share_stations_no_free_production`), higher meaning more congestion; `zone_inputs` and `municipality_inputs` choose which feed each level. A zone's raw value is the input's statistic over the whole analysis window, from the latest feature run that has it; the population is the matching per-local-day statistic of every zone in that same run, pooled.
> - Zones in a feature run (`scanner.features.run.run_zones`): the run country's zones and `reference_zones`, then every zone of the countries with status `active` or `reference`. Each zone's day-ahead price comes from the source its country file's `precedence` selects (ENTSO-E alone for NL), so every active zone with prices gets F1 and F2's non-positive price share, and those populations pool every such zone. Zones of `draft` countries (IT, HU) stay out until their country files are signed off; the reference set in the assumption file that D1 foresees does not exist yet. Local days and regime sub-windows are the run country's for every zone; input rows follow each zone's own country's market rules.
> - Percentile = mean rank: 100 × (values below + half the values equal) / n. Ties share a percentile.
> - F7 has no daily values. For zones, its percentile is taken among the zones that have the statistic; with DK1 and DK2 only this is 25 or 75, the degenerate case described above.
> - Weights: `score_weights_v0`, renormalised to sum to 100 over the inputs present for the entity (`zone_missing_input_policy` and `municipality_missing_input_policy` = `renormalise`; the alternative `null_component` keeps a missing input as a null, flagged component and scales the weights over all the level's inputs). The breakdown lists the absent ones.
> - Scored entities: zones of countries with status `active` that have at least one input. Reference zones only widen populations.
> - Municipality site scores: a municipality inherits its parent zone's percentiles for every day-based input unchanged. The F7 component is replaced by the municipality's own `share_stations_no_free_production`, ranked among the country's municipalities that have it. A municipality without F7 values is scored on the inherited inputs alone (renormalised), so it ranks level with its zone's other municipalities that lack them. Scenarios can score sites for load-type or production-type assets instead (`municipality_grid_direction`; `f7_direction_aware`, `f7_mw_weighted`), which reads a direction-aware F7 statistic; a municipality whose F7 rests on fewer than `municipality_min_stations` stations is flagged `low_station_count` and still ranked (`docs/methods/municipality_site_score.md`).
> - `feat.score.breakdown`: `features` maps each feature key (as in `feat.feature_value`) to `raw`, `percentile`, `weight` (renormalised), `contribution` (weight × percentile / 100; contributions sum to the score), plus `weight_key`, `set_weight`, `statistic`, `run_id`, `period`, `population` and, for municipalities, `inherited_from`. Top level: `window`, `level`, `method` (ID and version), `population` (text), `percentile_method`, `missing_input_policy`, `missing_features`, `missing_inputs` and `flags` (e.g. `no_station_data`), `assumption_sets` (ID, content hash, role, proposed flag and parameters, parameters the scenario replaced), `scenario` (ID, content hash, status), `proposed_assumptions`, `labels`, `capacity` (the F7 area statistics used by the opportunity rules) and `parent_zone` for municipalities. Missing components under `null_component` have null `raw`, `percentile` and `contribution` and a `flag`.

## Grid-friendliness score (Phase 3, open decision D4)

Per asset type and substation: can it get a flexible connection, can it share a connection point, does its typical flow relieve or worsen the local constraint (charging on surplus at a production-saturated node relieves; firm load at a consumption-saturated node worsens), and an estimate of queue wait. Specified in full before WP3.10.

## Opportunity type rules

Rules use percentiles from the score breakdown and thresholds from `assumptions/opportunity_rules_v0.yaml`.

- BESS: high F1, high F5, substation not saturated in the relevant direction.
- DSF: high F5 and industrial load nearby (needs Layer 2 or F12; until then shown as "candidate, needs site data").
- Modular DC: high F2 and F4, substation saturated for production with room for consumption.

A zone or substation can match several types; the card shows all matches ordered by the return range midpoint where one exists.

> **MVP provisional, pending H7** (implemented in `scanner.scores.opportunity`): rules are evaluated per scored zone and municipality on its congestion-score breakdown. "High" means the feature's percentile is at least `high_percentile`. Zones and municipalities are areas, not substations, so the substation conditions use the area's F7 statistics: "not saturated in a direction" means at least one station in the area has free capacity in that direction (`share_stations_no_free_<direction>` < 1), "saturated" means at least one has none (share > 0). BESS needs one direction not saturated. These are existence tests on area aggregates; per-station joint conditions (one station saturated for production yet with room for consumption) wait for substation scores. Each condition records its value, operator and threshold and is passed, failed or undecided (missing data). Status: `no_match` if a condition failed or a feature percentile is missing; `match` if all passed; `candidate_needs_site_data` if nothing failed and only site data is missing (industrial load nearby, which has no source yet, or F7 statistics).

## Return range v0 (MVP)

Defined in `docs/valuation.md`, section "BESS v0". Output is low and high EUR per MW per year per zone, labelled "zone-level, perfect-foresight upper bound, day-ahead arbitrage only".

Implemented in `scanner.valuation.run`: one `val.run` and `val.result` row per zone with day-ahead prices, the run country's zones and its reference zones (for Denmark DK1, DK2, DE-LU, NO2, SE3, SE4). A local day with any missing or null interval is skipped and counted, never filled. Municipalities get no rows of their own: a site card shows its parent zone's range, joined in the serving view on `ref.entity.parent_id`, with the zone-level labels.

## Estimates (Phase 5)

Each estimate method gets a section here with inputs, formula, known biases and the proxies it is cross-checked against. Start with node saturation and curtailment per area for Italy and Hungary.
