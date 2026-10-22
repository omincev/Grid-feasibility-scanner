# UI contract: serving views for the hackathon front end

What the Lovable UI reads, and nothing else: the views in the `serving` schema, over HTTP through PostgREST
as the anonymous role, or the same data as static files in `exports/`. Every value carries the run ID that
produced it (`ops.compute_run` for derived data, `ops.ingest_run` for capacity-map facts). Views return
public rows only. Migrations: `db/migrations/20261018090000_serving_contract.sql`,
`..._serving_contract_spatial.sql`, `20261018120100_serving_scenarios.sql`, `..._spatial.sql`,
`20261018140000_f7_site_cards.sql` and `20261018150000_source_completeness.sql`;
`tests/test_serving.py` checks the column lists below against the database.

## Scenarios

Scores, opportunity types and return ranges are computed once per scenario (`assumptions/scenarios/`): a named
set of analytics assumptions (weights, method choices, rules, battery template). Exactly one scenario is
official. The card and map views (`zone_map`, `zone_card`, `zone_feature_breakdown`, `ranked_sites`,
`opportunities`, `return_ranges`) show the official scenario's latest complete run, one consistent run per
view, and say which in `scenario_id`. Other scenarios are for experts comparing hypotheses:
`scenario_comparison` has every scored entity under every scenario with the differences to the official one,
`scenario_catalog` lists the scenarios, `assumption_register` every parameter behind them and
`method_catalog` every method card (`docs/methods/`). A readable version of these is
`exports/methods_and_scenarios.html`. Features and the substation map are scenario-independent.

## Connect

The six raw-data views need a token ("Raw data (token required)"); everything else needs none.

Local base URL: `http://localhost:3001` (`POSTGREST_PORT` in `.env`, default 3001). No credentials: requests
run as role `anon`, which can read `serving` and nothing else. Start it with `docker compose up -d db
postgrest` (first time: `infra/README.md`, "Local stack"). On Supabase later the base URL becomes
`https://<project>.supabase.co/rest/v1`, with headers `apikey: <anon key>` and `Accept-Profile: serving`.

PostgREST syntax: `select=` picks columns, `<column>=eq.<value>` filters (also `in.(a,b)`, `is.true`,
`gte.`), `order=<column>.asc|desc`, `limit=`. Arrays and JSON come back as JSON.

```sh
BASE=http://localhost:3001
curl "$BASE/zone_card?country_code=eq.dk&select=entity_id,short_name,congestion_score,best_fit_asset_types,revenue_low_eur_per_mw_year,revenue_high_eur_per_mw_year,revenue_labels,labels"
curl "$BASE/zone_feature_breakdown?entity_id=eq.dk.zone.dk1&order=display_order.asc&select=title,raw_value,unit,percentile,weight,contribution,caveat"
curl "$BASE/ranked_sites?order=rank.asc&limit=10&select=entity_id,name,zone_name,rank,rank_in_zone,congestion_score,is_benchmark"
curl "$BASE/ranked_sites?is_benchmark=is.true"
curl "$BASE/substation_map?zone_or_area_id=eq.dk.zone.dk2&select=entity_id,name,geometry,status_production,free_mw_production,publication_date"
curl "$BASE/source_freshness?kind=in.(manifest,loader)&order=source.asc,dataset.asc"
curl "$BASE/source_completeness?complete=not.is.true&select=source,dataset,partitions_incomplete,partitions_missing,missing_partitions"
```

```js
const BASE = "http://localhost:3001";
const get = (path) => fetch(`${BASE}/${path}`).then((r) => r.json());
const zones = await get("zone_map?select=entity_id,short_name,congestion_score,labels,geometry");
// Geometries are GeoJSON objects in a JSON column; build the FeatureCollection client side:
const fc = { type: "FeatureCollection", features: zones.map(({ geometry, ...p }) => ({ type: "Feature", id: p.entity_id, geometry, properties: p })) };
const card = (await get("zone_card?entity_id=eq.dk.zone.dk1"))[0];
```

`substation_map` with every column is about 5 MB (the `capacity` and `attributes` JSON): always `select=` the
columns a view needs. `zone_map` geometries are about 1 MB for DK1 and DK2 together.

## Static fallback

`uv run python -m scanner.cli export` (also the last step of `pipeline`) writes every serving view to
`exports/`: `zone_map.geojson`, `substation_map.geojson` and `ranked_sites.geojson` as GeoJSON
FeatureCollections (feature `id` = `entity_id`, all other columns in `properties`), every other view as
`<view>.json` with `{"rows": [...]}`, and `exports/index.json` with `generated_at` (UTC), the file list with
row counts, `runs` (= `serving.latest_runs`) and `labels` (every label found in the data). The export runs as
role `anon`, so it holds the same rows as the API. Copy the folder into the app's static assets for the demo.
The same step writes `exports/methods_and_scenarios.html` (self-contained, for readers who are not engineers)
and its markdown twin `.md`: scenarios, scenario comparison, assumption register and method cards
(`scanner.cli report` writes only these two).

## Conventions

- Timestamps are UTC ISO 8601 strings; convert to Europe/Copenhagen only for display. Periods are half-open
  `[start, end)`.
- Null means unknown or not published. Never show it as 0.
- `entity_id` values: `dk.zone.dk1`, `dk.municipality.<slug>`, `dk.substation.<dso|energinet>.<slug>`.
- `share` = fraction from 0 to 1; `ratio` = dimensionless; `EUR/MW/year` per MW of battery power.
- `*_run_id` columns trace a value to its run (`serving.latest_runs` lists the latest per kind and name).

## Labels the UI must show

| Text (exact) | Read from | Show it |
|---|---|---|
| `zone-level` | `revenue_labels` | next to every EUR/MW/yr range, including site cards (a site shows its zone's range) |
| `perfect-foresight upper bound`, `day-ahead arbitrage only` | `revenue_labels` | next to every range (render as "perfect-foresight upper bound, day-ahead arbitrage only") |
| `less than one year of data` or `no complete 365-day window` | `revenue_labels` | next to the range when present; then low = high (an annualised mean, not a range) |
| `proposed assumptions (not approved)` | `labels`, `score_labels`, `revenue_labels`; flag `proposed_assumptions` | card header badge, and next to the range when in `revenue_labels` |
| `unverified data` | `labels`; flag `unverified_data`; counts `score_unverified_values`, `revenue_unverified_values` | card header badge |
| `stale source` | `labels`; flags `stale_source`, `stale_datasets`; `source_freshness.stale` | badge, with the dataset names |
| `proxy` | `zone_feature_breakdown.caveat` (F2) | beside the F2 row |
| `current snapshot` | `caveat` (F7), `f7_caveat` | beside the F7 row, in substation popups, with site F7 figures |
| capacity map caveat | `capacity_map_caveat` (the source's Danish wording, verbatim), `capacity_map_caveat_en` (our translation) on `ranked_sites` and `substation_map` | on every site card and substation popup that shows capacity-map figures; show the Danish text, or the English one marked as a translation |
| known biases | `zone_card.revenue_known_biases` (from the method card) | under the range on the zone card |
| `no_station_data` | `score_flags` (zone_card, ranked_sites), `flag` (breakdown rows); flag `ranked_sites.no_station_data` | beside a site score whose F7 is null: no capacity-map stations (with values in the directions the statistic needs) in the municipality |
| `low_station_count` | `score_flags` (ranked_sites, scenario_comparison); flag `ranked_sites.low_station_count`; count `f7_sample_stations` | beside a site score whose F7 rests on fewer stations than the scenario's minimum (3 proposed): the site is ranked, but one station decides its F7 |

`revenue_label_text` is `revenue_labels` joined with ", " for a one-line caption. `labels` holds the card-level
badges only; the revenue and caveat labels are separate.

## Views

### `serving.zone_map`

One row per zone with a polygon (DK1, DK2): the map layer. Static file: `zone_map.geojson`.

| Column | Type | Unit | Meaning |
|---|---|---|---|
| `entity_id` | text | | zone ID |
| `name` | text | | zone name |
| `short_name` | text | | DK1, DK2 |
| `country_code` | text | | `dk` |
| `geometry` | json | | GeoJSON MultiPolygon, EPSG:4326 |
| `congestion_score` | float | 0-100 | congestion score; null if not scored |
| `period_start` | timestamptz | | start of the score's analysis window |
| `period_end` | timestamptz | | end of the window (exclusive) |
| `score_run_id` | uuid | | score run |
| `best_fit_asset_types` | text[] | | asset types whose status is `match`, best first; often empty |
| `revenue_low_eur_per_mw_year` | float | EUR/MW/year | BESS v0 range, low |
| `revenue_high_eur_per_mw_year` | float | EUR/MW/year | BESS v0 range, high |
| `revenue_labels` | text[] | | labels of the range (see above) |
| `revenue_label_text` | text | | the same, joined |
| `revenue_run_id` | uuid | | valuation run |
| `proposed_assumptions` | boolean | | score, opportunity or revenue used proposed assumptions |
| `unverified_data` | boolean | | the score or the range used unverified values; false when none are recorded |
| `stale_source` | boolean | | a dataset feeding the zone is stale |
| `labels` | text[] | | card-level badges |
| `scenario_id` | text | | scenario of the score and range (the official one) |

### `serving.zone_card`

One row per zone with a congestion score. Everything the zone card shows.

| Column | Type | Unit | Meaning |
|---|---|---|---|
| `entity_id` | text | | zone ID |
| `name` | text | | zone name |
| `short_name` | text | | DK1, DK2 |
| `country_code` | text | | country |
| `eic` | text | | ENTSO-E area code |
| `congestion_score` | float | 0-100 | weighted sum of feature percentiles |
| `period_start` | timestamptz | | analysis window start |
| `period_end` | timestamptz | | analysis window end (exclusive) |
| `score_labels` | text[] | | labels of the score |
| `missing_features` | text[] | | weight keys without data (F3, F6 until Phase 3); weights were renormalised without them |
| `weights_set_id` | text | | assumption set of the weights |
| `score_run_id` | uuid | | score run |
| `score_computed_at` | timestamptz | | when the score was computed |
| `breakdown` | json | | raw score breakdown (populations, assumption sets and hashes); display from `zone_feature_breakdown` |
| `best_fit_asset_types` | text[] | | asset types with status `match`, ordered by revenue midpoint where one exists |
| `asset_types` | json | | every asset type: `{asset_type, title, status, status_label, matched, conditions}`, matches first |
| `opportunity_run_id` | uuid | | opportunity-type run |
| `revenue_low_eur_per_mw_year` | float | EUR/MW/year | BESS v0 range, low |
| `revenue_high_eur_per_mw_year` | float | EUR/MW/year | BESS v0 range, high |
| `revenue_unit` | text | | `EUR/MW/year` |
| `revenue_labels` | text[] | | labels of the range |
| `revenue_label_text` | text | | the same, joined |
| `revenue_known_biases` | text[] | | biases of the method (docs/valuation.md) |
| `revenue_method` | text | | valuation method ID |
| `revenue_window_start` | timestamptz | | price window start |
| `revenue_window_end` | timestamptz | | price window end (exclusive) |
| `revenue_days_valued` | integer | days | local days with complete prices |
| `revenue_skipped_days` | integer | days | local days skipped (missing or null prices), never filled |
| `revenue_complete_windows` | integer | | complete 365-day windows behind low and high |
| `revenue_run_id` | uuid | | valuation run |
| `valuation_run_id` | uuid | | `val.run` row (inputs, parameters) |
| `revenue_computed_at` | timestamptz | | when the range was computed |
| `score_proposed_assumptions` | boolean | | the score used proposed weights |
| `opportunity_proposed_assumptions` | boolean | | the rules used proposed thresholds |
| `revenue_proposed_assumptions` | boolean | | the battery used proposed parameters |
| `proposed_assumptions` | boolean | | any of the three |
| `unverified_data` | boolean | | either count below is above 0; false otherwise (including when neither was recorded) |
| `score_unverified_values` | integer | values | unverified input values the feature runs behind the score loaded (all zones of those runs: percentiles pool them) |
| `revenue_unverified_values` | integer | values | unverified prices the valuation used for this zone |
| `stale_source` | boolean | | `stale_datasets` is not empty |
| `stale_datasets` | text[] | | `source.dataset` names feeding the zone whose `source_freshness.stale` is true |
| `data_through` | timestamptz | | end of the data window behind the score |
| `labels` | text[] | | card-level badges |
| `scenario_id` | text | | scenario of the score, types and range (the official one) |
| `score_flags` | text[] | | missing-input flags of the score, e.g. `no_station_data`; empty when every input has data |

### `serving.zone_feature_breakdown`

One row per zone and score component. Contributions sum to `congestion_score`.

| Column | Type | Unit | Meaning |
|---|---|---|---|
| `entity_id` | text | | zone ID |
| `feature_key` | text | | feature output, e.g. `f1_intraday_spread_hourly` |
| `raw_value` | float | `unit` | the zone's value over the analysis window |
| `percentile` | float | 0-100 | percentile in the reference population |
| `weight` | float | % | weight in this score (renormalised over present features, sums to 100) |
| `contribution` | float | points | weight × percentile / 100 |
| `run_id` | uuid | | score run |
| `weight_key` | text | | feature group, e.g. `f1_price_spread` |
| `statistic` | text | | statistic of `feature_key` used |
| `title` | text | | human title |
| `group_title` | text | | feature group title |
| `unit` | text | | unit of `raw_value` |
| `caveat` | text | | `proxy` (F2), `current snapshot` (F7), else null |
| `description` | text | | one-line definition |
| `set_weight` | float | | weight in the assumption set before renormalising |
| `period_start` | timestamptz | | period of `raw_value` |
| `period_end` | timestamptz | | |
| `population_method` | text | | `pooled_daily_values` (daily values of all zones) or `peer_areas` (F7: other zones) |
| `population_n` | integer | | size of the reference population |
| `feature_run_id` | uuid | | feature run behind `raw_value` |
| `unverified_values` | integer | values | unverified input values that feature run loaded (all zones of the run) |
| `display_order` | integer | | sort key (F1, F2, F4, F5, F7) |
| `scenario_id` | text | | scenario of the score (the official one) |
| `flag` | text | | why the component is null (`no_station_data`, `no_feature_data`), else null |

### `serving.substation_map`

One row per listed substation (DSO and TSO stations of the national capacity map) as a point. Static file:
`substation_map.geojson`. Colour by the factual status only: owners have not set MW thresholds yet.

| Column | Type | Unit | Meaning |
|---|---|---|---|
| `entity_id` | text | | station ID |
| `name` | text | | station name |
| `zone_or_area_id` | text | | bidding zone |
| `geometry` | json | | GeoJSON Point |
| `capacity` | json | | every current capacity fact by codebook entry: `{num, text, json, unit, known_at, valid_from, run_id}` |
| `attributes` | json | | source station code, voltage, DSO, municipality |
| `f7` | json | | F7 statistic → value for the station |
| `municipality_id` | text | | municipality the point lies in |
| `dso_area_id` | text | | DSO licence area (null for TSO stations or when ambiguous) |
| `network_level` | text | | `distribution` (DSO map) or `transmission` (TSO map) |
| `voltage_kv` | text | kV | as published (`60/10`, `150`) |
| `free_mw_production` | float | MW | free capacity for new production: the lowest published value that applies (F7) |
| `free_mw_consumption` | float | MW | free capacity for new consumption, same rule; null for transmission stations (`consumption_not_assessed`) |
| `status_production` | text | | `free_capacity` (> 0), `no_free_capacity` (= 0 as published), `unknown` (not published) |
| `status_consumption` | text | | same for consumption; `unknown` for transmission stations (`consumption_not_assessed`) |
| `limited_consumption_mw_low` | float | MW | limited-access consumption range, lower bound |
| `limited_consumption_mw_high` | float | MW | upper bound |
| `limited_solar_mw_low` | float | MW | limited-access solar range (200 interruptible hours a year), lower bound |
| `limited_solar_mw_high` | float | MW | upper bound |
| `limited_wind_mw_low` | float | MW | limited-access wind range, lower bound |
| `limited_wind_mw_high` | float | MW | upper bound |
| `limited_range_inverted` | boolean | | a published lower bound is above its upper bound (kept as published); then do not show the pair as a range, and do not show an upper bound of 0 as "0 MW" or as "unlimited" (meaning unknown, asked of Energinet) |
| `rank_in_zone_production` | float | | 1 = least free production MW among the zone's stations of the same level; ties share |
| `rank_in_zone_consumption` | float | | same for consumption |
| `f7_caveat` | text | | `current snapshot` |
| `f7_run_id` | uuid | | F7 run |
| `publication_date` | date | | latest publication date of the station's figures |
| `capacity_run_ids` | uuid[] | | capacity-map ingest runs that wrote the current facts |
| `consumption_not_assessed` | boolean | | true for transmission stations: every TSO row publishes 0 MW consumption, contradicted by its other fields, so F7 treats the figure as not assessed (inferred, pending Energinet; `capacity` keeps the facts as published). Show consumption as unknown, never as 0 MW |
| `capacity_map_caveat` | text | | the capacity map's caveat, verbatim (Danish) from the StoryMap |
| `capacity_map_caveat_en` | text | | the same in English, our translation (marked as such in the text) |

### `serving.ranked_sites`

Site cards: one row per scored municipality (98, benchmarks included), ranked by congestion score. Static
file: `ranked_sites.geojson`. Municipalities have no return range of their own: the revenue columns are the
parent zone's, labelled `zone-level`.

| Column | Type | Unit | Meaning |
|---|---|---|---|
| `entity_id` | text | | municipality ID |
| `name` | text | | municipality name |
| `kind` | text | | `municipality` |
| `parent_id` | text | | parent entity (the zone) |
| `point` | json | | GeoJSON Point on the municipality's surface |
| `congestion_score` | float | 0-100 | site score |
| `breakdown` | json | | components `{feature_key, weight_key, statistic, title, unit, caveat, raw_value, percentile, weight, contribution, inherited_from, feature_run_id, unverified_values}` |
| `revenue_low_eur_per_mw_year` | float | EUR/MW/year | parent zone's range, low |
| `revenue_high_eur_per_mw_year` | float | EUR/MW/year | parent zone's range, high |
| `revenue_labels` | text[] | | labels of that range |
| `rank` | bigint | | rank among all sites (1 = highest score; ties share a rank) |
| `zone_id` | text | | zone of the site |
| `zone_name` | text | | |
| `rank_in_zone` | bigint | | rank within the zone |
| `site_count` | bigint | | sites ranked |
| `site_count_in_zone` | bigint | | sites ranked in the zone |
| `period_start` | timestamptz | | analysis window start |
| `period_end` | timestamptz | | analysis window end (exclusive) |
| `score_run_id` | uuid | | score run |
| `score_computed_at` | timestamptz | | |
| `score_labels` | text[] | | labels of the score |
| `best_fit_asset_types` | text[] | | as on the zone card |
| `asset_types` | json | | as on the zone card |
| `opportunity_run_id` | uuid | | |
| `f7_stations` | float | stations | DSO stations in the municipality |
| `f7_share_no_free_production` | float | share | stations with 0 MW free production capacity |
| `f7_share_no_free_consumption` | float | share | stations with 0 MW free consumption capacity |
| `f7_free_mw_production_sum` | float | MW | sum of station values, indicative only (the source says they cannot be added) |
| `f7_free_mw_consumption_sum` | float | MW | same for consumption |
| `f7_caveat` | text | | `current snapshot` |
| `f7_run_id` | uuid | | F7 run |
| `revenue_entity_id` | text | | zone whose range is shown |
| `revenue_label_text` | text | | joined revenue labels |
| `revenue_run_id` | uuid | | valuation run |
| `is_benchmark` | boolean | | a benchmark site (Ringkøbing-Skjern, Lolland) |
| `benchmark_id` | text | | benchmark ID |
| `proposed_assumptions` | boolean | | score, opportunity or zone range used proposed assumptions |
| `unverified_data` | boolean | | the site's score or the zone's range used unverified values; false when none are recorded |
| `stale_source` | boolean | | the parent zone has a stale source |
| `labels` | text[] | | card-level badges |
| `attributes` | json | | entity attributes (e.g. `kommunekode`) |
| `score_unverified_values` | integer | values | unverified input values behind the site's score |
| `revenue_unverified_values` | integer | values | unverified prices behind the zone's range |
| `scenario_id` | text | | scenario of the site score (the official one) |
| `score_flags` | text[] | | missing-input flags, e.g. `no_station_data` (F7 null, see the method card), and `low_station_count` |
| `capacity_map_caveat` | text | | the capacity map's caveat, verbatim (Danish) from the StoryMap: show it with the site's F7 figures |
| `capacity_map_caveat_en` | text | | the same in English, our translation (marked as such in the text) |
| `no_station_data` | boolean | | the site's F7 is null: no capacity-map station with the values the statistic needs |
| `low_station_count` | boolean | | the site's F7 rests on fewer stations than the scenario's minimum (`score_method.municipality_min_stations`); the site is still scored and ranked |
| `f7_sample_stations` | float | stations | the station count the site's F7 statistic rests on (e.g. stations with a published production value); null without F7 |

### `serving.source_freshness`

One row per dataset: manifests (time series), snapshots (captures of current-state pages) and runtime
loaders without a manifest (the capacity map).

| Column | Type | Unit | Meaning |
|---|---|---|---|
| `source` | text | | source ID (`snapshots` for captures) |
| `source_name` | text | | source name from its dossier |
| `dataset` | text | | dataset or snapshot ID |
| `kind` | text | | `manifest`, `snapshot` or `loader` |
| `country_code` | text | | manifest country (`eu` for ENTSO-E) |
| `registry_status` | text | | manifest status (`active`, `backfill_only`) or snapshot status (`active`, `to_research`) |
| `schedule` | text[] | | cron expressions |
| `schedule_timezone` | text | | |
| `resolution` | text | | ISO 8601 duration or `per_period` |
| `licence` | text | | |
| `freshness_sla` | text | | ISO 8601 duration from the manifest |
| `freshness_sla_hours` | float | h | the same in hours |
| `last_success_at` | timestamptz | | end of the last successful run or capture |
| `last_success_run_id` | uuid | | that ingest run |
| `last_success_partition` | text | | its local day |
| `last_success_quarantined` | boolean | | its rows were quarantined (soft deviation) |
| `hours_since_success` | float | h | now minus `last_success_at` |
| `last_attempt_at` | timestamptz | | start of the last run or capture of any status |
| `last_attempt_status` | text | | `ok`, `blocked`, `failed`, `running` |
| `last_attempt_run_id` | uuid | | |
| `rows_partition` | text | | newest local day with a completeness check |
| `rows_expected` | integer | rows | rows a complete day should have |
| `rows_received` | integer | rows | rows received for that day |
| `rows_complete` | boolean | | received ≥ expected |
| `last_changed_at` | timestamptz | | snapshots: when the capture last changed |
| `data_through` | timestamptz | | latest interval end held for the dataset |
| `stale` | boolean | | last success older than the SLA; null without an SLA |

### `serving.source_completeness`

One row per time-series dataset (manifest): the local days of its history up to yesterday and how many of
them were received in full, from `ops.ingest_run`. `source_freshness` shows only the newest checked day; this
view shows the gaps inside the history. A day counts by its best successful run (stored rows are never deleted):
`complete` if one received at least the rows it expected, `incomplete` if every checked run fell short,
`unchecked` if no successful run had an expected count (a run on the day itself, or a resolution that cannot
be counted), `missing` if no run of that day succeeded. Today is left out (incomplete by nature).

| Column | Type | Unit | Meaning |
|---|---|---|---|
| `source` | text | | source ID |
| `source_name` | text | | source name from its dossier |
| `dataset` | text | | dataset |
| `country_code` | text | | manifest country (`eu` for ENTSO-E) |
| `registry_status` | text | | manifest status (`active`, `backfill_only`) |
| `first_partition` | date | | first local day of the window: the manifest's history start |
| `last_partition` | date | | last local day: yesterday in the country's timezone, or the history end |
| `partitions_expected` | integer | days | local days in the window |
| `partitions_complete` | integer | days | days received in full |
| `partitions_incomplete` | integer | days | days with fewer rows than expected |
| `partitions_unchecked` | integer | days | days with a successful run but no completeness check |
| `partitions_missing` | integer | days | days without a successful run |
| `rows_expected` | bigint | rows | rows the checked days should hold |
| `rows_received` | bigint | rows | rows received for them |
| `incomplete_partitions` | text[] | | the incomplete days (`YYYY-MM-DD`), oldest first |
| `missing_partitions` | text[] | | the missing days, oldest first |
| `complete` | boolean | | no incomplete or missing day; null when some days are unchecked and none is short |

### `serving.scenario_catalog`

One row per scenario: what a scenario switcher or a methods page lists.

| Column | Type | Unit | Meaning |
|---|---|---|---|
| `scenario_id` | text | | scenario ID |
| `title` | text | | title |
| `status` | text | | `official` (exactly one) or `exploratory` |
| `description` | text | | what the scenario assumes and why, in plain words |
| `content_hash` | text | | SHA-256 of the scenario file as loaded; outputs record it |
| `assumption_hashes` | json | | `{set_id: content hash}` of the sets it names |
| `sets` | json | | `{role: set_id}` |
| `method_switches` | json | | `{"<role>.<parameter>": value}`: method choices it changes |
| `overrides` | json | | `{"<role>.<parameter>": value}`: other values it changes |
| `owners` | text[] | | owners |
| `approved_by` | text | | approver of the scenario's own values; null: they count as proposed |
| `approved_on` | date | | |
| `score_run_id` | uuid | | latest complete congestion-score run |
| `opportunity_run_id` | uuid | | latest complete opportunity-type run |
| `valuation_run_id` | uuid | | latest complete valuation run |
| `source_path` | text | | file in git |
| `loaded_at` | timestamptz | | when `load-curated` loaded it |

### `serving.assumption_register`

One row per parameter of every assumption set: the register to show field experts.

| Column | Type | Unit | Meaning |
|---|---|---|---|
| `set_id` | text | | assumption set |
| `set_kind` | text | | e.g. `score_weights`, `score_method`, `asset_template.bess` |
| `set_status` | text | | `proposed` or `approved` |
| `parameter` | text | | parameter name |
| `value` | json | | approved value; null until an owner sets it |
| `proposed` | json | | proposed value, used only when proposed assumptions are allowed (outputs then say so) |
| `note` | text | | what it means and where the proposal comes from |
| `owners` | text[] | | owners of the set |
| `status` | text | | `value_set`, `proposed_only` or `missing` |
| `approved_by` | text | | set approver |
| `approved_on` | date | | |
| `scenarios` | text[] | | scenarios that use the set |
| `scenario_values` | json | | `{scenario_id: value}` where a scenario replaces this parameter; null if none does |
| `roles` | text[] | | roles the set is used for |
| `content_hash` | text | | SHA-256 of the set file as loaded |
| `source_path` | text | | file in git |

### `serving.method_catalog`

One row per method card (`docs/methods/<method_id>.md`).

| Column | Type | Unit | Meaning |
|---|---|---|---|
| `method_id` | text | | method ID, as recorded on every output row |
| `version` | integer | | method version |
| `title` | text | | title |
| `status` | text | | `provisional` or `approved` |
| `owner` | text[] | | owners |
| `inputs` | text[] | | codebook classes or upstream outputs it reads |
| `parameters` | text[] | | `<role>.<parameter>` it resolves through the scenario |
| `outputs` | text[] | | tables and views it writes or feeds |
| `known_biases` | text[] | | known biases |
| `body` | text | | the formula in plain words (markdown) |
| `content_hash` | text | | SHA-256 of the card file |
| `source_path` | text | | file in git |
| `loaded_at` | timestamptz | | |

### `serving.scenario_comparison`

One row per scored entity (zones and municipalities) and scenario: score, rank and BESS range side by side, with
the differences to the official scenario (this scenario minus the official one; 0 on the official scenario's
own rows). A municipality shows its zone's range.

| Column | Type | Unit | Meaning |
|---|---|---|---|
| `entity_id` | text | | zone or municipality |
| `name` | text | | |
| `kind` | text | | `zone` or `municipality` |
| `zone_id` | text | | the zone (itself for zones) |
| `scenario_id` | text | | scenario |
| `scenario_title` | text | | |
| `scenario_status` | text | | `official` or `exploratory` |
| `congestion_score` | float | 0-100 | score in this scenario |
| `rank` | bigint | | rank among entities of the same kind in this scenario (1 = highest; ties share) |
| `rank_in_zone` | bigint | | rank within the zone |
| `revenue_low_eur_per_mw_year` | float | EUR/MW/year | BESS v0 range in this scenario, low |
| `revenue_high_eur_per_mw_year` | float | EUR/MW/year | high |
| `official_scenario_id` | text | | the official scenario |
| `official_congestion_score` | float | 0-100 | score in the official scenario |
| `official_rank` | bigint | | rank in the official scenario |
| `score_delta` | float | points | score minus the official score |
| `rank_delta` | bigint | places | rank minus the official rank (positive: placed lower) |
| `rank_in_zone_delta` | bigint | places | same within the zone |
| `revenue_low_delta` | float | EUR/MW/year | low minus the official low |
| `revenue_high_delta` | float | EUR/MW/year | high minus the official high |
| `score_flags` | text[] | | missing-input flags, e.g. `no_station_data` |
| `proposed_assumptions` | boolean | | the score used proposed values |
| `score_run_id` | uuid | | score run |
| `revenue_run_id` | uuid | | valuation run |
| `revenue_entity_id` | text | | zone whose range is shown |
| `is_benchmark` | boolean | | a benchmark site |

### Supporting views

`serving.feature_catalog`: titles, units, caveats and descriptions of the score components (`weight_key`,
`feature_key`, `statistic`, `group_title`, `title`, `unit`, `caveat`, `description`, `display_order`); rows
with a null `statistic` describe a whole feature group, including F3 and F6, which are not computed yet.

`serving.latest_runs`: per derived-data run kind, name and scenario, the latest finished run (`kind`, `name`,
`run_id`, `status`, `started_at`, `finished_at`, `code_version`), the latest attempt (`last_attempt_status`,
`last_attempt_at`) and `scenario_id` (null for feature runs, which every scenario shares, and for runs written
before scenarios existed).

`serving.opportunities`: the official scenario's latest opportunity run, one row per entity and asset type
(`entity_id`, `asset_type`, `matched`, `status`, `reasons`, `run_id`, `computed_at`, `scenario_id`);
`reasons.conditions` lists each rule condition with value, operator, threshold and result.

`serving.return_ranges`: the official scenario's latest valuation run, one row per entity, asset type and
metric, including the reference zones (DE-LU, NO2, SE3, SE4) (`entity_id`, `asset_type`, `metric`, `low`,
`mid`, `high`, `unit`, `labels`, `method`, `method_version`, `template_set_id`, `run_id`, `computed_at`,
`valuation_run_id`, `price_window_start`, `price_window_end`, `proposed_assumptions`, `days_valued`,
`skipped_days`, `complete_365_day_windows`, `unverified_values`, `scenario_id`).

## Raw data (token required)

Six more views in `serving` expose the stored data behind the cards. They are for holders of a shared bearer
token only: role `authenticated`, not `anon`; without a valid token PostgREST answers 401 or 403 (permission
denied). They are not in the static export. Like every view they return `public` rows only. Migration:
`db/migrations/20261021090000_serving_raw.sql`. Columns are plain and stable (add, never rename).

Auth: send `Authorization: Bearer <token>`. An operator mints a token with `scanner token --days N` (default 14;
`infra/README.md`, "Raw data access"). A token in browser code is visible to everyone who opens the page.

Row cap and paging: one response holds at most `API_MAX_ROWS` rows (default 50000), silently cut off at the cap.
Page with `limit=` and `offset=` and a stable `order=`; send `Prefer: count=exact` to get the total in the
`Content-Range` header.

- `serving.series`: one row per time series (`series_key`, `entity_id`, `entity_name`, `entity_kind`,
  `country_code`, `entry_id`, `source`, `dataset`, `field`, `unit`, `resolution` as an interval string such as
  `01:00:00` or `00:15:00`, `kind` = actual, forecast, settlement or capacity, `licence`).
- `serving.observations`: values of a series (`series_key`, `interval_start`, `interval_end` UTC and half-open,
  `value` null when the source published none, `issued_at` for forecasts, `ingested_at`, `review_status`, `run_id`).
  Per (series, interval, issued_at) the latest ingest only, as `core.observation_effective` defines it; usable rows
  only: `review_status` is `ok`, `unverified` (quarantined, nobody decided; show "unverified data") or `verified`;
  rows of a run a person rejected are left out. Always filter on `series_key` and `interval_start`
  (`gte.`/`lt.`): that uses the index and stays fast.
- `serving.entities`: `entity_id`, `kind`, `country_code`, `parent_id`, `name`, `eic`. Geometry stays in `zone_map`
  and `substation_map`.
- `serving.facts`: sparse facts, with history, such as capacity-map values per station (`fact_id`, `entity_id`,
  `attribute`, `value_num`, `value_text`, `value_json`, `unit`, `valid_from`, `valid_to`, `known_at`, `source`,
  `source_url`, `run_id`). Take the newest `known_at` per `entity_id` and `attribute` for the current value.
- `serving.feature_values`: every stored feature value of every run (`feature_key`, `entity_id`, `period_start`,
  `period_end`, `statistic`, `value`, `value_text`, `run_id`, `method_id`, `method_version`, `computed_at`); several
  runs hold the same period, so filter on `run_id` (see `latest_runs`) or take the newest `computed_at`.
- `serving.scores_all`: congestion scores of every scenario's latest score run (`scenario_id`, `entity_id`,
  `score_kind`, `score` 0 to 100, `breakdown` JSON with the components, `period_start`, `period_end`,
  `weights_set_id`, `run_id`, `computed_at`). The card views show the official scenario only.

```sh
BASE=http://localhost:3001
TOKEN=$(uv run python -m scanner.cli token --days 7)
curl -H "Authorization: Bearer $TOKEN" "$BASE/series?entity_id=eq.dk.zone.dk1&field=ilike.*DayAhead*&select=series_key,unit,resolution,kind"
# DK1 day-ahead prices for one week (use a series_key from the first call), 1000 rows per page:
curl -H "Authorization: Bearer $TOKEN" "$BASE/observations?series_key=eq.<series_key>&interval_start=gte.2026-10-01T00:00:00Z&interval_start=lt.2026-10-08T00:00:00Z&order=interval_start.asc&limit=1000&offset=0&select=interval_start,value,review_status"
```

## Known gaps

- Substation colours: owners have not set MW thresholds (`saturation_thresholds_v0` covers percentages and
  categories, but the map publishes MW). Only the factual status is served.
- A published 0 MW means below the publisher's first step (under 10 MW for consumption, under 20 MW for
  production), not exactly zero. For DSO stations the transmission limit is not applied (it needs an
  approved DSO-to-TSO station link), so the true free capacity can be lower.
- Transmission (TSO) consumption capacity is served as unknown: all 298 TSO rows publish 0 MW, which their own
  "hours at 0 MW" field (also 0) contradicts, and the source does not say what the 0 means. This reading is
  ours, pending Energinet's answer (`docs/questions/capacity-map-semantics.md`); the published facts are kept.
- The 70 DSO stations with an inverted limited-access range (`limited_range_inverted`) keep their values as
  published; an upper bound of 0 there is neither 0 MW nor "unlimited", and no F7 statistic reads it.
- Site scores in the exploratory scenarios `f7_direction_aware` and `f7_mw_weighted` are for load-type assets
  (BESS charging, modular DC, DSF): their F7 is the share (or MW) of stations with free consumption capacity
  where production is saturated. Compare them in `scenario_comparison`; the card views show `demo_default`.
- Score weights, opportunity thresholds (`high_percentile` 75) and battery parameters are proposed, not
  approved: every score, type and range carries `proposed assumptions (not approved)`. With the proposed
  thresholds no zone or municipality currently matches any asset type, so `best_fit_asset_types` is empty;
  show `asset_types` with their status instead. DSF stays `candidate, needs site data` at best until a
  source for industrial load exists.
- Municipalities inherit their zone's day-based percentiles; only F7 differs between them, so many sites tie
  (equal score, shared rank).
- When the return range is labelled `less than one year of data` or `no complete 365-day window`, low equals
  high: it is an annualised mean (whether windows of 365 valued days should count is pending H7).
- Unverified values are quarantined values (soft fingerprint deviation) that no person has accepted or
  rejected; every computation uses them and records how many it used (`docs/quality.md` layer 2). The score
  count covers every zone of the feature runs behind it, so DK1 and DK2 show the same score count. Outputs
  computed before the counts existed have null counts and `unverified_data` false.
- `stale` compares the last successful run with the manifest SLA. Snapshots and the capacity map have no SLA
  (`stale` null). A zone's `stale_source` looks at every dataset of the source its country file prefers per
  measure, which can flag a dataset the features did not use.
- `source_completeness` counts each day by the runs of that day's partition. Scheduled runs ingest the current
  day, which has no completeness check, so days after the last backfill stay `unchecked` until a run for them
  as a past day (for example `scanner.cli ingest --from <day>`); rows a neighbouring day's run wrote through its
  lookback or lookahead are not counted for the day.
- Only DK1 and DK2 have polygons; reference zones appear in `return_ranges` only. Municipalities are served
  as points (no polygons).
- F3 interconnector saturation and F6 renewable pipeline are Phase 3 (`missing_features`).
- Scores, types and ranges written before scenarios existed (`scenario_id` null) are not served; run `pipeline`
  (every scenario: `--all-scenarios`) to fill the views. Scenario values that are not approved count as proposed
  values, so outputs that use them carry `proposed assumptions (not approved)`.
- In the official scenario (`demo_default`) F7 is not part of the zone score, so zone cards show four components;
  site scores keep F7, and sites without capacity-map stations have a null F7 (`no_station_data`) and a lower
  score instead of renormalised weights.
