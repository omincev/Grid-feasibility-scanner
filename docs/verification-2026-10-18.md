# Verification report, 18 October 2026

The `verify` agent ran these checks read-only. Sources checked:

- the `scanner` database (localhost:5432);
- PostgREST as `anon` (localhost:3001);
- `exports/`, generated 2026-10-18T10:28:17Z.

Live calls, made one at a time:

- Energi Data Service (EDS): 9 calls, around 10:30Z;
- the national capacity map ArcGIS service `Kapacitetskortet_V2`: 3 calls (service index, layers 0 and 1).

ENTSO-E was **not** checked against its source, because that needs a token.

**State of the database during the check.** The pipeline ran twice:

| Run | Time | Code version |
|---|---|---|
| 1 | 10:15Z | `41cfa85` |
| 2 | 10:27Z | `fc6fd09` |

Both runs gave identical values. Views and exports point to run 2:

- score `19ebd29b`, opportunities `d1d7042a`, valuation `93028c40`;
- features `b10b0dc8` (F1), `33206704` (F2), `016f12fe` (F4), `8ccf7641` (F5), `759412cf` (F7).

The ENTSO-E EnergyPrices backfill fix also completed during the check.

## Summary

| # | Check | Result |
|---|---|---|
| 1 | H2 spot check (F1, F2, F4, F5, F7) | **PASS.** All 25 hand-picked values match (tables below). My own independent code, run on live source data, also reproduces every stored DK daily value (13,026), every window statistic, every percentile and both zone scores. Largest difference: 2e-12. |
| 2 | Coverage | **PASS, with source gaps.** Stored EDS raw values equal the source row for row. Every gap and partial day is also missing at the source. The two blocked `Forecasts_Hour` days are a real source gap. The ENTSO-E EnergyPrices gap was confirmed, then fixed during the check. |
| 3 | Plausibility | **PASS, with caveats.** I solved the BESS model independently and got exactly the stored ranges, and a spread-based estimate agrees. The scores follow arithmetically from the breakdown, but DK2 ranks above DK1 only because of the n=2 F7 percentile. The ties are explained in section 3. |
| 4 | Serving and export | **PASS.** All 10 views are served with exactly the documented columns. The labels are present. `core`, `feat`, `val`, `ops` and `ref` are closed to `anon`. The exports are valid and equal the views. Both benchmarks are present and traceable. |
| 5 | Traceability | **PASS.** Every run ID served (including those inside JSON) resolves to an `ok` row in `ops.compute_run`, `ops.ingest_run` or `val.run`. |

## 1. H2 spot check

**Method.** I downloaded the full window (2025-03-04 to 2026-10-18) for DK1 and DK2 from EDS:

- DayAheadPrices;
- Elspotprices;
- ImbalancePrice;
- Forecasts_Hour, field ForecastDayAhead;
- GenerationProdTypeExchange.

I then recomputed `docs/features.md` with my own plain-Python code, not the project's polars code. For F7 I recomputed from the live ArcGIS layers, using PostGIS to find the municipality of each source point. "Stored" means the value in the latest feature run. Every pair below differs by less than 1e-9.

### F1 price spread

| Value | Zone | Local day | Why picked | Stored | Recomputed |
|---|---|---|---|---|---|
| `f1_intraday_spread_native` daily_spread | DK1 | 2025-03-30 | DST spring (23 h) | 75.099999 | 75.099999 |
| `f1_intraday_spread_native` daily_spread | DK2 | 2025-09-30 | last PT1H day | 271.310005 | 271.310005 |
| `f1_intraday_spread_hourly` daily_spread | DK1 | 2025-10-01 | first PT15M day, resampled | 273.967505 | 273.967505 |
| `f1_intraday_spread_native` daily_spread | DK2 | 2025-10-26 | DST autumn (100 quarter-hours) | 50.139999 | 50.139999 |
| `f1_interzone_spread` daily_mean | DK1 | 2026-03-29 | DST spring (92 quarter-hours) | 1.361522 | 1.361522 |

### F2 curtailment proxy

| Value | Zone | Local day | Why picked | Stored | Recomputed |
|---|---|---|---|---|---|
| `f2_nonpositive_price_share` daily_share | DK1 | 2025-03-30 | DST spring | 0.434783 | 0.434783 |
| `f2_nonpositive_price_share` daily_share | DK2 | 2025-10-01 | resolution switch | 0.0 | 0.0 |
| `f2_curtailment_proxy_mwh` daily_sum | DK1 | 2025-05-11 | sunny Sunday | 701.577525 | 701.577525 |
| `f2_curtailment_proxy_mwh` daily_sum | DK2 | 2025-10-26 | DST autumn (25 h) | 0.0 | 0.0 |
| `f2_curtailment_proxy_mwh` daily_sum | DK1 | 2025-11-24 | source publishes only null forecasts | no row | no row |

### F4 renewable surplus index

| Value | Zone | Local day | Why picked | Stored | Recomputed |
|---|---|---|---|---|---|
| `f4_surplus_index` daily_mean | DK1 | 2025-03-30 | DST spring (23 h) | -0.098414 | -0.098414 |
| `f4_surplus_index` daily_mean | DK2 | 2025-10-26 | DST autumn (25 h) | -0.488119 | -0.488119 |
| `f4_surplus_index` daily_mean | DK1 | 2025-10-01 | resolution switch | -0.502284 | -0.502284 |
| `f4_surplus_index` daily_share_above_0 | DK2 | 2026-03-29 | DST spring 2026 | 0.0 | 0.0 |
| `f4_surplus_index` daily_mean | DK1 | 2026-06-21 | summer | -0.321772 | -0.321772 |

### F5 balancing activation

| Value | Zone | Local day | Why picked | Stored | Recomputed |
|---|---|---|---|---|---|
| `f5_activation_share_up` daily_share | DK2 | 2025-03-04 | first day, 47 of 96 quarter-hours | 0.042553 | 0.042553 |
| `f5_activation_share_up` daily_share | DK1 | 2025-03-30 | DST spring (92 quarter-hours) | 0.380435 | 0.380435 |
| `f5_activation_share_down` daily_share | DK2 | 2025-10-26 | DST autumn (100 quarter-hours) | 0.59 | 0.59 |
| `f5_imbalance_spot_spread` daily_mean | DK1 | 2025-10-01 | resolution switch | 124.699271 | 124.699271 |
| `f5_imbalance_spot_spread` daily_mean | DK2 | 2026-03-04 | regime break (dominating-direction basis) | 77.519271 | 77.519271 |

### F7 substation saturation

Live ArcGIS layers; the publication date is 2026-09-01 unless stated.

| Value | Source fields | Recomputed | Stored |
|---|---|---|---|
| `dso.lolland.nak_nakskov` free_mw_production | without TSO transformer 40, with it 40 | 40 | 40 |
| `dso.faaborg_midtfyn.fby_faaborg_by` free_mw_consumption | 60 and 0, so the lower value applies | 0 | 0 |
| `dso.aarhus.has_hasle` production / consumption (its name is shared with a station on Bornholm) | 100/100 and 60/60 | 100 / 60 | 100 / 60 |
| `energinet.mal_150` free_mw_production | existing grid 42 (the reinforced-grid 385 is not used); published 2026-02-01 | 42 | 42 |
| `municipality.ringkoebing_skjern` share_stations_no_free_production | 23 of 29 stations at 0 MW | 0.793103 | 0.793103 |

**F7 in full:**

- all 937 DSO stations match;
- all 140 TSO stations match (existing grid);
- all 96 municipalities match, Lolland included (5 of 12 = 0.416667);
- both zones match: DK1 441 of 733 = 0.601637, DK2 136 of 203 = 0.669951.

### Window statistics and scores

I recomputed these from the source intervals. Percentiles use the stored daily values of the reference zones from the same run.

| | DK1 stored = recomputed | DK2 stored = recomputed |
|---|---|---|
| F1 hourly spread, mean (EUR/MWh) | 130.3055 | 134.5150 |
| F2 non-positive share | 0.055196 | 0.028617 |
| F4 surplus index, mean | -0.262858 | -0.601193 |
| F5 imbalance-spot spread, mean (EUR/MWh) | 65.2143 | 74.5974 |
| Percentiles F1 / F2 / F4 / F5 / F7 | 67.82 / 82.57 / 73.95 / 59.11 / 25 | 69.98 / 80.94 / 33.81 / 67.62 / 75 |
| Congestion score | 59.3961 | 66.0677 |

## 2. Coverage

### Raw values: source compared with stored

DK1 and DK2, from 2025-03-04 local to the time of the check.

| Dataset | Source rows | Stored | Value differences |
|---|---|---|---|
| DayAheadPrices | 73,536 | 73,536 | 0 |
| Elspotprices | 10,126 | 10,126 | 0 |
| ImbalancePrice (ImbalancePriceEUR, SpotPriceEUR, DominatingDirection) | 113,846 | 113,842 | 0 |
| GenerationProdTypeExchange (4 fields) | 28,484 | 28,484 | 0 |
| Forecasts_Hour ForecastDayAhead (3 types) | 84,888 | 84,888 | 0 |

The 4 ImbalancePrice rows missing from storage are today's intervals 11:30 and 11:45 local. The source published them after the last ingest.

### EDS series per local day

Both zones look the same.

| Series | First interval (UTC) | Last interval end (UTC) | Complete days | Partial days | Missing days | DST days |
|---|---|---|---|---|---|---|
| DayAheadPrices PT15M | 2025-09-30 22:00 | 2026-10-18 22:00 | 383 of 383 | 0 | 0 | 2025-10-26: 100, 2026-03-29: 92 |
| Elspotprices PT1H | 2025-03-03 23:00 | 2025-09-30 22:00 | 211 of 211 | 0 | 0 | 2025-03-30: 23 |
| ImbalancePrice PT15M (10 fields) | 2025-03-04 11:15 | 2026-10-18 09:30 | 592 | 2025-03-04 (47 of 96, the source starts at 12:15 local); today (46 of 96) | 0 | 92 / 100 / 92 |
| GenerationProdTypeExchange PT1H (4 fields) | 2025-02-17 23:00 | 2026-10-18 09:00 | 607 | today (11 of 24) | 0 | 23 / 25 / 23 |
| Forecasts_Hour ForecastDayAhead PT1H | 2025-03-01 23:00 | 2026-10-18 22:00 | 588 to 591 | see note | 2025-11-22 and 2025-11-23 (all types); 2025-09-23 (onshore); 2026-05-07 (offshore) | 23 / 25 / 23 |

**Forecasts_Hour partial days:**

| Local day | Hours present (of 24) |
|---|---|
| 2025-04-10 | 14 |
| 2025-08-30 | 14 (wind) |
| 2025-09-23 | 7 (solar) |
| 2025-09-24 | 23 |
| 2025-11-24 | 8 to 9 |
| 2026-05-08 | 11 (offshore) |

**Explicit nulls, per zone:**

- ForecastDayAhead: 53 solar, 83 onshore, 119 offshore;
- DominatingDirection: 16 (DK1), 17 (DK2);
- SatisfiedDemand: 31.

Every gap above has the same number of rows at the source.

**The two blocked Forecasts_Hour days are a real source gap.**

- EDS returns `total: 0` for 2025-11-21T23:00Z to 2025-11-23T23:00Z, even with no zone filter.
- 2025-11-24 has 50 of 144 rows, and their ForecastDayAhead is null.
- Blocking the run on zero rows was correct.

**Effect on F2.** `f2_curtailment_proxy_mwh` has no value on 10 local days. On each day the source either has no forecast or publishes nulls; nothing is filled:

- 2025-04-10;
- 2025-08-30;
- 2025-09-23 and 2025-09-24;
- 2025-11-09;
- 2025-11-22, 2025-11-23 and 2025-11-24;
- 2026-05-07 and 2026-05-08.

### ENTSO-E (not checked against its source)

| Series | Stored | Finding |
|---|---|---|
| EnergyPrices | PT60M from 2025-03-03 to 2025-09-30; PT15M from 2025-10-01 | **Gap confirmed.** At the start of the check, DK had no data after local day 2026-08-07. The runs for partitions 2026-08-07 to 2026-10-18 had failed with `hard:parse:two values for eu.entsoe.EnergyPrices.no.zone.no2.price_amount.pt15m`. **Fixed during the check:** 73 runs succeeded from 10:19Z, and all 15 zones are now complete from 2025-03-04 to 2026-10-18. Oddity: NO2 has PT15M data back to March 2025, plus a stray PT60M series (48 rows, 2025-05-10 and 2025-05-16). Danish features do not use this series, because EDS has precedence. |
| ActualTotalLoad DK1/DK2, PT1H | 2025-03-01 23:00 to 2026-10-18 08:00 | 12 partial days. These include the DST day 2025-10-26 (21 of 25 hours; 23:00 to 03:00Z missing) and 2026-10-12 to 2026-10-14. |
| ActualGenerationPerType DK | PT60M up to 2025-04-09, PT15M from 2025-04-08 | The two series overlap on 2 days. Partial days: 8 to 53 per type in DK1; 91 for DK2 solar, including 2026-03-29 (52 of 92). |

No feature uses ActualTotalLoad or ActualGenerationPerType (their classes are `total_load_actual` and `generation_actual_power`).

## 3. Plausibility

### BESS v0

The battery: 2 h, 91% round trip split evenly into charge and discharge, state of charge 0.1 to 0.9, starting and ending each day at 0.5, at most one cycle of discharge per day.

| EUR/MW/yr | DK1 | DK2 |
|---|---|---|
| Stored range (229 complete 365-day windows, 0 days skipped) | 57,566 to 72,231 | 63,239 to 76,307 |
| My own daily LP (scipy HiGHS) on the downloaded source prices | 57,566 to 72,231 (exact) | 63,239 to 76,307 (exact) |
| Spread estimate: 1.526 MWh/day (1.6 MWh usable × √0.91) × the stored hourly daily spread, summed over each 365-day window | 60,617 to 72,072 | 65,331 to 75,458 |
| Annualised mean over all 593 days | 70,828 | 73,908 |

Notes on the range:

- The low end is the first window (2025-03-04 to 2026-03-03). The high end is the window ending 2026-10-12.
- Measured against 1.526 MWh × the native daily spread, the LP captures 88% of it, which is reasonable.

Published comparators (none of them gives a day-ahead-only figure for Denmark):

| Source | Published figure | Ours |
|---|---|---|
| [Electricity Maps, 2025 review](https://www.electricitymaps.com/grid-in-review-2025/denmark) | average daily price swing in Denmark 2025: EUR 116/MWh | 117.6 (DK1) and 121.3 (DK2), hourly, 2025-03-04 to 2025-12-31 |
| [Rabobank, Nordic BESS ranking](https://www.rabobank.com/knowledge/d011535313-backup-power-for-europe-part-8-ranking-bess-market-attractivity-in-the-nordics) | past-year spread EUR 120/MWh (DK1) and 126/MWh (DK2). Unverified: seen only in a search snippet, the page returned 403 and its definition is unknown. | 128.8 and 135.0, last 365 days, hourly |
| [Clean Horizon index via Emaldo](https://emaldo.com/insights/emaldo-market-review-the-danish-ancillary-services-market) | DK1 full stack (day-ahead, intraday, FCR, aFRR, mFRR) 100 to 300 kEUR/MW/yr for a 4 h asset | A day-ahead-only 2 h result well below the full stack fits |

**Verdict:** a plausible upper bound for day-ahead arbitrage alone.

### Congestion scores

Each contribution is weight × percentile / 100. The weights are the proposed set renormalised over the five features present: 18.75 each for F1, F2, F4 and F5, and 25 for F7.

| Component | DK1 percentile | DK1 points | DK2 percentile | DK2 points |
|---|---|---|---|---|
| F1 hourly spread | 67.82 | 12.72 | 69.98 | 13.12 |
| F2 non-positive share | 82.57 | 15.48 | 80.94 | 15.18 |
| F4 surplus | 73.95 | 13.86 | 33.81 | 6.34 |
| F5 imbalance spread | 59.11 | 11.08 | 67.62 | 12.68 |
| F7 share of stations with 0 MW free production | 25 | 6.25 | 75 | 18.75 |
| **Score** | | **59.40** | | **66.07** |

The scores follow exactly from the breakdown. Three caveats, all pending H7 and D1, not code bugs:

1. **F7 decides the order.** With only two zones, F7's percentile can only be 25 or 75, which is worth 12.5 points. On F1, F2, F4 and F5 alone, DK1 leads 53.15 to 47.32. The F7 shares behind the 25/75 split are close: 0.60 and 0.67.
2. **F2 barely separates zones.** 80% of the pooled daily non-positive shares are 0. Any zone with some non-positive hours therefore lands above 80: DK1 gets 82.6 and DK2 80.9, although DK1's share is about twice DK2's.
3. **Window statistics are compared with daily values.** For right-skewed statistics this pushes percentiles up. For example, the pooled F1 median is 98.5, while the DK means are 130 to 135.

### Municipality ties at rank 1 (74.5)

Why the scores tie:

- A municipality inherits its zone's percentiles for F1, F2, F4 and F5 unchanged. Only F7 differs, and it is ranked among the 96 municipalities that have stations.
- 28 municipalities have every station at 0 MW free production (share = 1.0): 9 in DK1 and 19 in DK2. They share an F7 percentile of 100 × (68 + 28/2) / 96 = 85.42.
- A DK1 site with share 1.0 therefore scores 59.40 − 6.25 + 0.25 × 85.42 = 74.50. These are Fanø, Læsø, Langeland, Middelfart, Nordfyns, Nyborg, Odder, Svendborg and Syddjurs. Fanø and Læsø have one station each.
- The 19 DK2 sites with share 1.0 tie at 68.67, rank 18.

Overall there are 46 distinct scores, with 65 sites in 13 tie groups. I recomputed all 96 site scores with F7 data and they all match.

Two side effects:

- **Sites without F7 data rank above sites that have it.** Dragør and Ishøj have no stations. They are scored on the inherited features alone, renormalised, which gives 63.09 (rank 56). That puts them above every DK2 municipality whose stations have free capacity (F7 percentile below 63).
- **Benchmarks:** Ringkøbing-Skjern ranks 37 (score 68.64, 23 of 29 stations saturated) and Lolland ranks 83 (54.22). Their expected ranges in `benchmarks/dk.yaml` are still null, so there is nothing to compare against.

## 4. Serving and export

**Views.** All 10 views in `docs/ui-contract.md` are served over HTTP. The documented columns are exactly the columns in PostgREST's OpenAPI: none missing, none extra.

| View | Rows | View | Rows |
|---|---|---|---|
| zone_map | 2 | source_freshness | 62 |
| zone_card | 2 | feature_catalog | 14 |
| zone_feature_breakdown | 10 | latest_runs | 8 |
| substation_map | 1,139 | opportunities | 300 |
| ranked_sites | 98 | return_ranges | 6 |

**Labels.**

- `revenue_labels` holds `zone-level`, `perfect-foresight upper bound`, `day-ahead arbitrage only` and `proposed assumptions (not approved)`, on both zone cards and all 98 sites.
- `labels` holds `proposed assumptions (not approved)` and `unverified data`.
- F2's caveat is `proxy`; F7's caveat and every station's `f7_caveat` are `current snapshot`.
- `revenue_known_biases` has 4 entries.
- No `stale source` label appears, which is correct: no dataset is stale.
- `exports/index.json` lists 7 labels.

**Access.** PostgREST answers `PGRST106` (406) for `core`, `feat`, `val`, `ops`, `ref` and `public`. In the database, `SET ROLE anon` gets "permission denied for schema" on `core`, `feat`, `val`, `ops` and `ref`, and can read `serving`.

**Exports.**

- All 11 files parse.
- The 3 GeoJSON files are FeatureCollections whose feature `id` equals `entity_id`. Geometries are inside Denmark (longitude 8.07 to 15.16, latitude 54.56 to 57.75). Danish letters are valid UTF-8.
- Every file equals its view, except time-dependent `source_freshness` columns that changed after the export (`hours_since_success`, last runs).

**Benchmarks.** Both appear in `ranked_sites` with `is_benchmark` true:

| Site | Benchmark ID |
|---|---|
| Ringkøbing-Skjern | `dk.site.ringkobing_skjern` |
| Lolland | `dk.site.lolland` |

Their run IDs are score `19ebd29b`, valuation `93028c40`, F7 `759412cf` and opportunities `d1d7042a`. All four resolve to `ok` compute runs.

## 5. Traceability

I collected every UUID served by the 10 views, including those nested in JSON (breakdown `run_id` and `feature_run_id`, capacity facts' `run_id`, `capacity_run_ids`).

| Kind of run ID | Distinct IDs | Resolves to |
|---|---|---|
| Score, opportunity, valuation and feature run IDs (`score_run_id`, `opportunity_run_id`, `revenue_run_id`, `feature_run_id`, `f7_run_id`, `run_id`, `latest_runs`) | 8 | `ops.compute_run`, all `ok` |
| `valuation_run_id` | 6 | `val.run`. Its `run_id` equals `revenue_run_id`, and low/high equal `val.result`. |
| Capacity facts' `run_id` and `capacity_run_ids` | 1 | `ops.ingest_run` (capacity map, `ok`) |
| `source_freshness` last success and last attempt | 9 | `ops.ingest_run`, all `ok` |

Unresolved: 0.

## Issues found

1. **`ops.compute_run.finished_at` is wrong** (`src/scanner/runtime/provenance.py:89`).
   - It is set with `now()`. In PostgreSQL that returns the start of the transaction, and here the transaction opens with the run's first input query.
   - So every run appears to last 3 to 20 ms. For example, F1 run `b10b0dc8` actually took about 2.7 s.
   - `serving.latest_runs.finished_at` is wrong as a result. Fix: use `clock_timestamp()`. `started_at` defaults to `now()` as well.
2. **The congestion order depends on the degenerate F7 percentile, and F2 barely separates zones** (section 3). This is an H7/D1 decision to make before the demo story says "DK2 is more congested".
3. **Sites without F7 data (Dragør, Ishøj) outrank sites with real free capacity.** The renormalisation follows the spec, but it should be shown or flagged in the UI.
4. **The live API will show every source as stale on demo day.** SLAs run from 2 h (ImbalancePrice) to 36 h, and the API computes staleness with `now()`. If schedules are not running, every zone card will show `stale source` by 21 October. The exports are frozen and will show not stale. Re-run ingest and the pipeline before the demo, or serve the exports.
5. **`source_freshness` only looks at the newest partition.** Interior holes are invisible: the ENTSO-E EnergyPrices hole showed as fresh and complete while it existed.
6. **One DSO station is unlocated.** `dk.substation.dso.unlocated.mav_masnedoe` lies in no simplified municipality polygon, so it has no zone and is left out of the municipality and zone F7 statistics (936 of 937 counted).
7. **ENTSO-E quality, unverified without a token:**
   - DST and partial-day gaps in ActualTotalLoad and ActualGenerationPerType;
   - overlapping PT60M and PT15M generation series (2025-04-08 and 2025-04-09);
   - NO2 EnergyPrices at PT15M before the 15-minute day-ahead go-live, plus a stray PT60M series.

   No current feature uses these series, but they must be fixed before ENTSO-E becomes an input.
