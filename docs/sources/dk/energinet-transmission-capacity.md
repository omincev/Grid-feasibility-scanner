# Source dossier: Energinet transmission capacity information

Status: draft. Country: dk. Owner: Misi. Last verified: 2026-10-18 (live inspection of the ArcGIS services, the StoryMap and the Energi Data Service metadata).

## What it is

Energinet publishes capacity information for its own grid (132/150 kV stations and the 400 kV backbone) in four forms, all part of "Kapacitetskort for elnettet" (the same map family as the DSO capacity maps, see `dso-capacity-maps.md`):

1. **Station capacity (TSO side of the capacity map):** per transformer station at 132 and 150 kV, indicative free capacity for new production and new consumption, for the existing grid and for the grid after approved reinforcements. This is the main input for F7/F9 at transmission level.
2. **Free bays ("ledige felter"):** per station and per consumption/production area, whether a free bay exists for a new connection.
3. **Transmission line traces:** cable and overhead-line geometry with voltage (33 to 400 kV, HVDC). Useful as map context only.
4. **Connection-queue and the pause in new connections:** covered by `energinet-connection-queue.md` (another dossier). Not repeated here. Note for context: Energinet announced a temporary pause in new grid connections on 2026-03-02 and a new model for larger connections on 2026-05-27 (links in "Risks").

Data caveat from the StoryMap (verbatim, 2026-10-18): "TSO delen af Kapacitetskortet forventes ikke opdateret før primo 4 kvartal 2026." Transmission data was last updated 2026-02-01 (`publication_date` 1769904000000 in the ArcGIS layer; 2026-02-01 in EDS). The map text says the TSO part is updated monthly, but in practice it has been static since February 2026. Capturing it from now on starts the history clock.

## Access

Two equivalent routes, both without authentication.

### A. Energi Data Service (preferred for values)

- Method: REST API
- Endpoint: `https://api.energidataservice.dk/dataset/GridCapacityMapTSOProductionandConsumption` (dataset id 156)
- Metadata: `https://api.energidataservice.dk/meta/dataset/GridCapacityMapTSOProductionandConsumption`
- Predecessor (discontinued, production only, still queryable): `GridCapacityMapTSO` (id 111; 4418 rows over 14 monthly publications from 2024-06-01 to 2025-12-01: 2024-06, 09, 10, 11, 12, 2025-01, 02, 03, 05, 06, 07, 09, 11, 12; about 317 to 320 rows each, 298 for the last two). A usable monthly history for backfilling production capacity at transmission stations (different columns and no consumption; map via the codebook, not by name equality).
- Response: `{total, limit, dataset, records: [...]}`; filter column `publication_date`; resolution P1M; update frequency P1M.
- Authentication: none. Rate limits: the API returns HTTP 429 with a `Retry-After` header after bursts (hit during research after about 25 calls in a few minutes). Poll once per day at most; one `limit=5000` request fetches everything.
- Other agents calling EDS from the same IP share the limit.

### B. ArcGIS FeatureServer (geometry, free-bay layer, line traces)

- Service: `https://services7.arcgis.com/an1TuZOdFp9M3PVx/arcgis/rest/services/Kapacitetskortet_V2/FeatureServer` (item `cb063f4772ab4ca9958b7c77b7ab84d5`, owner `it_gis_ENDK`, last modified 2026-04-15, public).
- Layers on the TSO side (layer ids are positional; read them by name from `?f=json` at runtime, see the DSO dossier for the rule):

| id | name | geometry | rows | notes |
|---|---|---|---|---|
| 0 | `TSO_stations_capacity_points` | point | 298 | 140 rows `existing_grid` and 158 rows `reinforced_grid`; voltages 150 kV (160 rows) and 132 kV (138 rows) |
| 3 | `TSO_stations_capacity_polygons` | polygon | 297 | same attributes as layer 0 plus `SHAPE__Area`, `SHAPE__Length`; station supply areas |
| 2 | `TSO_stations_fields_points` | point | 186 | free bays; 167 `Aktuel`, 19 `Fremtidig` |
| 4 | `TSO_stations_fields_polygons` | polygon | not counted | consumption-dominated vs production-dominated area, per station |

- Line traces: `https://services7.arcgis.com/an1TuZOdFp9M3PVx/arcgis/rest/services/Transmissionsnettet_Kapacitetskort/FeatureServer`
  - layer 0 `Transmissionsnet_Kabeltrace` (polyline, 4220 rows): `byggenavn` (string), `spaending` (string: 400, 220, 150, 132, 60, 33, 10, HVDC, Elektrode, UD), `globalid`.
  - layer 1 `Transmissionsnet_Luftledningstrace` (polyline, 367 rows): `byggenavn`, `driftsspænding` (double: 132, 150, 220, 250, 400; 11 nulls), `globalid`.
- Query parameters that work, no token: `where=1=1&outFields=*&outSR=4326&resultRecordCount=2000&f=json` (or `f=geojson`; with `f=geojson` and `outSR=4326` coordinates are lon,lat). Native spatial reference is EPSG:25832; `outSR=4326` reprojects server-side. `maxRecordCount` is 2000, enough for every layer except the cable traces (4220: page with `resultOffset`).
- Item metadata has no `licenseInfo` or `termsOfUse`.
- Web experiences that wrap these layers (not needed for ingestion): "Kort 3 og 7 - Transmissionsnettet" `0452b87b14974d27abf5469a5cf6ad41`, "Kort 4 og 8" `cfbf68fc0f63480faefdca3e1681cd94`, "Ledige felter" `42366494532f4b318826f9ed4d6c5476` (all under `https://experience.arcgis.com/experience/<id>`).
- Public landing page: `https://storymaps.arcgis.com/stories/eb5b387e376f49b8996d5e7c47fbdd37` ("Kapacitetskort for elnettet", authors Green Power Danmark and Energinet). `https://kapacitetskort.dk/` redirects to it. `https://www.energinet.dk/el/eltransmissionsnettet/kapacitetskort/` is a thin page pointing at kapacitetskort.dk.
- How it was found: AGOL search `https://www.arcgis.com/sharing/rest/search?q=Kapacitetskort&f=json`, then web map definitions `https://www.arcgis.com/sharing/rest/content/items/<id>/data?f=json` for the nine "Kapacitetskort N" web maps, which list the layer URLs and definition expressions (kort 3 = layer 3 or 0 with `grid_precondition = 'existing_grid'`, kort 4 = `'reinforced_grid'`, kort 9 = layer 2 and 4). No headless browser was needed.

Fixtures: `tests/fixtures/kapacitetskort/` (ArcGIS samples, layer metadata, line traces, EDS samples and meta). `eds_GridCapacityMapTSOProductionandConsumption__benchmark_stations.json` is a recorded empty result (`total: 0`): the filter used bare codes (`VID`) but EDS codes carry voltage suffixes (`ABA_150--$`, `ÅBØ_150`). It documents that exact-match filters on bare codes fail; match with the suffix or filter client-side.

## Datasets

### TSO station capacity (EDS `GridCapacityMapTSOProductionandConsumption`, ArcGIS layer 0/3)

One row per station and `grid_precondition`. Field names differ between the two routes; both are listed.

| EDS field | ArcGIS field | Meaning (source's definition, shortened) | Unit |
|---|---|---|---|
| `publication_date` | `publication_date` | Publication date of the capacities. EDS: ISO local string `2026-02-01T00:00:00`; ArcGIS: epoch ms UTC (`1769904000000` = 2026-02-01T00:00Z) | date |
| `TSO_station_name` | `TSO_station_name` | Long name of the TSO station | text |
| `TSO_station_abbreviation` | `TSO_station_abbreviation` | Station code including voltage, e.g. `MAL_150`, `ÅBØ_150`; EDS may carry suffixes such as `ABA_150--$` | text |
| `grid_precondition` | `grid_precondition` | "Either the existing transmission grid, or the reinforced transmission grid which includes approved reinforcements": `existing_grid` / `reinforced_grid` | enum |
| `voltage_level_kv` | `voltage_level_kv` | 132 or 150 (string in EDS, smallint in ArcGIS) | kV |
| `latitude`, `longitude` | geometry | EPSG:4326 (EDS); EPSG:25832 point (ArcGIS, reproject with `outSR=4326`) | deg |
| `available_capacity_production_MW` | `available_capacity_production_MW` | "The calculated capacity for production in MW. Usually an average over all hours of a typical climate year." Indicative, not additive across stations | MW |
| `cap_cons_MW` | `available_capacity_consumption_MW` | Same for consumption | MW |
| `hrs_zero_cons` | `count_of_hours_with_0MW_available_consumption` | Hours with 0 MW consumption capacity in a typical climate year | h |
| `cap_solar_100MW_pct`, `cap_solar_300MW_pct` | `available_capacity_solar_at_100_MW_in_percent`, `..._300_MW_in_percent` | Share of a new 100 / 300 MW solar plant's expected annual production that can be delivered to the grid (source text: "calculated as delivered/possible yearly wind production", sic) | percent |
| `cap_wind_100MW_pct`, `cap_wind_300MW_pct` | `available_capacity_wind_at_100_MW_in_percent`, `..._300_MW_in_percent` | Same for wind | percent |
| `cap_cons_su_0006_100MW_pct` ... `cap_cons_wi_2100_300MW_pct` (16 fields) | `available_capacity_consumption_{summer,winter}_{00_to_06,06_to_17,17_to_21,21_to_00}_at_{100,300}_MW_in_perc` | Share of a 100 / 300 MW consumption load that can be served in that time band. Summer = 1 April to 30 September, winter = 1 October to 31 March (both inclusive) | percent |

EDS on 2026-10-18 holds one publication, `2026-02-01T00:00:00`, 298 rows (140 `existing_grid`, 158 `reinforced_grid`; 160 stations at 150 kV and 138 at 132 kV), identical in size to the ArcGIS layer 0. Observed values on 2026-10-18 (298 ArcGIS rows): every consumption field is 0 at every station (`available_capacity_consumption_MW`, all 16 percent bands and `count_of_hours_with_0MW_available_consumption`). The loader stores them as published zeros (facts are never rewritten). Their meaning is undocumented and the fields contradict each other, so F7 treats the transmission consumption figures as not assessed, not as "no free consumption capacity" (inferred interpretation pending Energinet's answer; see "Verification of review claims"). Production MW ranges from 2 to 1286 (existing grid) and 54 to 2291 (reinforced grid) according to the web-map class breaks.

Time convention: `publication_date` is the publication (valid-from) date, not an observation interval. The capacities are "ledig kapacitet ... da Kapacitetskortet senest blev opdateret" (snapshot). Treat each publication as valid from `publication_date` until the next one; store the date as UTC midnight of the Danish calendar date.

### Free bays (ArcGIS layer 2/4 only; not in EDS)

| Field | Meaning |
|---|---|
| `TSO_station_abbreviation`, `TSO_station_name` | station |
| `available_fields_for_publication_capacitymap` | `Ja` (14), `Nej` (77), `Ikke afdækket` (95) = "not yet mapped" |
| `consumption_or_production_area` | `Forbrug` or `Produktion`: whether the area is dominated by consumption or production (decides the connection-payment size for new production). Layer renderer also lists `Ukendt` |
| `station_status` | `Aktuel` (167) or `Fremtidig` (19; planned station whose investment has the needed approvals) |
| `publication_date` | string `2026-04-01` (this layer only; a string, not a date) |
| `updated_date_for_number_of_fields` | string, `N.A` where unknown |
| `ENDK_timestamp` | server-side load timestamp (epoch ms), not a publication date |

Source note: "Ved kortets offentliggørelse den 1. februar 2026 manglede antallet af ledige felter at blive kortlagt på nogle af stationerne." The 400 kV stations are in this layer (abbreviation ends in `400`) but not in the capacity layer.

### Line traces

See Access. Geometry only, with voltage. No capacity values.

### Verification of review claims (2026-10-18)

**Station counts.** Layer 0 has 298 rows = 140 `existing_grid` + 158 `reinforced_grid`, 160 distinct stations (the loader's "298 rows -> 160 stations"; 20 stations exist only in the reinforced grid). Layer 2 (free bays) has 186 rows, all distinct codes. The union of codes is 202 entities `dk.substation.energinet.*`: 144 in both layers, 16 in the capacity layer only, 42 in the free-bay layer only (400 kV and planned stations among them). The "140 matched" are the stations with an `existing_grid` row.

**F7 does not ignore TSO stations.** `scanner.features.substation` (inputs `tso_consumption`, `tso_production` = `existing_grid` facts) writes station-level rows for the 140 existing-grid stations (`free_mw_production`, `rank_in_zone_production`; ranked among TSO stations only). Not covered: the 20 reinforced-only stations (the `reinforced_grid` facts are not an F7 input) and the 42 free-bay-only stations (no MW). The zone and municipality aggregates without suffix (`stations`, `share_stations_no_free_*`, `free_mw_*_sum`, and so on), and the default congestion score input, use DSO stations only (the BEK 1468 §29 population). Since F7 version 2 (2026-10-18) every area statistic except the sums also exists with the suffix `_incl_tso`, counting DSO and TSO existing-grid stations where their production value is published (`docs/methods/f7_substation_saturation.md`).

**F7 handling of TSO consumption (version 2, inferred, pending Energinet).** The published consumption figures stay in `core.fact` as published. F7 treats them as not assessed (`scanner.features.substation.NOT_ASSESSED`): TSO stations get no `free_mw_consumption` and no consumption rank, no area statistic counts them as stations without free consumption capacity (so the `_incl_tso` consumption and direction-aware statistics rest on DSO stations alone), and `serving.substation_map` shows their consumption status as `unknown` with `consumption_not_assessed` true. Each F7 run notes how many published values it set aside. To be lifted, or turned into "no capacity", when Energinet answers question 1 of `docs/questions/capacity-map-semantics.md`.

**TSO consumption = 0 everywhere.** All 298 rows have `available_capacity_consumption_MW` = 0, all 16 time-band percentages = 0 and `count_of_hours_with_0MW_available_consumption` = 0. Documentation: the EDS text says only "The calculated capacity for consumption in MW. Usually an average over all hours of a typical climate year." (`cap_cons_MW`, example value 136) and "The count of hours with 0 MW capacity for consumption in a typical climate year." (`hrs_zero_cons`, example value 3705). The StoryMap and EDS metadata (`https://api.energidataservice.dk/meta/dataset/GridCapacityMapTSOProductionandConsumption`) never say that 0 means "no free capacity" or "not assessed"; the StoryMap says only that "Kapacitetskortet for forbrug er udarbejdet til screening af ledig kapacitet til store forbrugsanlæg" and that the consumption maps for transmission are Kort 7 (existing) and 8 (future). Verdict: undocumented. Evidence against reading it as "no capacity": an average of 0 MW over a typical year implies zero capacity in all 8760 hours, yet `hrs_zero_cons` says 0 hours with zero capacity, and all percentages are 0 as well; the two statements contradict each other. This pattern looks like an unpopulated column (default 0), not a calculation. Inferred, not documented: treat the transmission consumption values as "not published / not assessed" (null) until Energinet answers; do not rank or aggregate them as "no free consumption capacity" (implemented in F7 version 2, see above). The February 2026 data is also the pre-update data (StoryMap: "TSO delen af Kapacitetskortet forventes ikke opdateret før primo 4 kvartal 2026"). Question 1 in `docs/questions/capacity-map-semantics.md`.

**Cadence.** StoryMap: "Transmissionsnettet: 1/2-2026 (opdateres hver måned)"; EDS `P1M`. In practice the data has been unchanged since 2026-02-01. Details and the display caveat: `dso-capacity-maps.md`.

## Decisions

The normalisation decisions for the transmission side of the capacity map, in one place (the asset `capacity/kapacitetskort` shows this section in Dagster next to the DSO dossier's):

- **One fact per station and grid precondition.** `existing_grid` and `reinforced_grid` rows are separate codebook entries (`...available_capacity_production_MW.existing_grid`, `...reinforced_grid`); F7 reads the existing grid only.
- **Valid from the publication date.** `publication_date` is the valid-from date of a snapshot, stored as UTC midnight of the Danish calendar date; each publication is valid until the next.
- **Percent fields are not ingested.** The solar, wind and consumption-band percentages and `hrs_zero_cons` have no equivalence class yet; they are never converted to MW or the reverse.
- **Published zeros stay zeros in the facts.** The all-zero transmission consumption values are stored as published; F7 treats them as not assessed (`scanner.features.substation.NOT_ASSESSED`, inferred, pending Energinet's answer to question 1 of `docs/questions/capacity-map-semantics.md`).
- **Free bays are categories as published** (`Ja`, `Nej`, `Ikke afdækket`), entry `...available_fields_for_publication_capacitymap`; TSO stations shared by the capacity and free-bay layers are one entity when the code is identical.

## Licence and terms

- ArcGIS items: `licenseInfo` and `termsOfUse` empty. No attribution requirement stated.
- EDS: terms are tracked in the `dk.eds.terms` snapshot (not owned by this dossier). Unverified here: the EDS general terms of use at `https://www.energidataservice.dk/` (open data, free use with source attribution is the platform's stated model).
- StoryMap disclaimer (verbatim): "Kapacitetskortet må på ingen måde tolkes som en forpligtigelse eller et tilsagn fra hverken netselskaberne eller Energinet." The values are indicative snapshots. Any product built on them must carry that wording or an equivalent.
- Redistribution in a paid product: not stated anywhere; ask Energinet (`netudvikling@energinet.dk`, the contact the StoryMap gives for data questions) if the product is sold. Marked unverified.

## Measured or estimated

Calculated, indicative figures from Energinet load-flow runs (N-1, 8760-hour climate-year scenario including connected plants, plants with screening or maturation agreements and politically decided projects). They are not measurements. Stations' values must not be summed. We store them as published and never convert percentages to MW or the reverse.

## Sensitivity

`public`.

## Codebook entries needed

See the proposal in `dso-capacity-maps.md` ("Proposed codebook mapping"), which covers both DSO and TSO fields. TSO entries:

- `dk.eds.GridCapacityMapTSOProductionandConsumption.available_capacity_production_MW` -> `capacity.free_mw.production`, unit MW, kind capacity, dimension `grid_precondition`.
- `dk.eds.GridCapacityMapTSOProductionandConsumption.cap_cons_MW` -> `capacity.free_mw.consumption`, unit MW, dimension `grid_precondition`.
- The percent fields (solar/wind 100/300 MW, 16 consumption band fields), `hrs_zero_cons` and free-bay status have no existing equivalence class. Proposed new classes are listed in the DSO dossier; do not map them to `free_mw`.

## Risks

- The TSO side has not been refreshed since February 2026 while the page text promises monthly updates; the StoryMap banner says the next update is not expected before early Q4 2026. Expect a format change in the update.
- Layer ids and field names can change (the service is `Kapacitetskortet_V2`, a rebuild of earlier services; the EDS dataset replaced `GridCapacityMapTSO` in February 2026). Always resolve layers by name.
- EDS and ArcGIS field names differ and EDS abbreviation strings contain suffixes; join on station abbreviation after normalising, and check coverage (both sources have 298 rows: 140 existing_grid and 158 reinforced_grid; 18 stations therefore appear only in the reinforced grid, presumably planned stations; unverified).
- Policy risk: Energinet paused new grid connections on 2026-03-02 (`https://energinet.dk/om-nyheder/nyheder/2026/03/02/energinet-indforer-midlertidig-pause-i-nye-nettilslutninger-og-saetter-turbo-pa-akutpakke/`) and announced a new model on 2026-05-27 (`https://energinet.dk/om-nyheder/nyheder/2026/05/27/energinet-indforer-ny-model-for-storre-tilslutninger-og-vil-forst-underskrive-nye-nettilslutningsaftaler-til-efteraret/`). The capacity numbers may be restructured around that model.

## Snapshots

`registry/snapshots.yaml`: `dk.energinet.transmission_capacity.eds.tso` (EDS JSON), `...arcgis.tso_stations`, `...arcgis.tso_fields`, `...arcgis.tso_areas`, `...lines_cable`, `...lines_overhead`, `...page`, plus the shared `dk.capacity_map.storymap` and `dk.capacity_map.arcgis.*` entries.
