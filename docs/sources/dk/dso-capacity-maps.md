# Source dossier: Danish DSO capacity maps

Status: draft. Country: dk. Owner: Misi. Last verified: 2026-10-18 (live inspection of the ArcGIS services, the StoryMap, DSO websites and the Energi Data Service metadata).

## What it is

Under BEK nr 1468 of 1 December 2025 (netvirksomhedsbekendtgørelsen) §29, every Danish DSO must publish a digital map viewer showing, for stations with a primary voltage of 25 to 99 kV, the capacity available for new consumption and production connections, the areas each station supplies, and whether each area has free, limited or no capacity for new connections with full network access. The map must show whether flexible connection agreements are possible, must state the calculation criteria, and must be updated at least every three months.

**Key finding: there is one shared map, not 40.** All DSOs meet §29 through a single national "Kapacitetskort for elnettet", built by Energinet and Green Power Denmark on ArcGIS Online (StoryMap `https://storymaps.arcgis.com/stories/eb5b387e376f49b8996d5e7c47fbdd37`, alias `https://kapacitetskort.dk/`). The StoryMap cites BEK 1468 itself: "I henhold til Bekendtgørelse om varetagelse af netvirksomhedsaktiviteter (BEK 1468 af 1/12/2025) skal netvirksomheder offentliggøre en digital kortviser ... Disse oplysninger formidles på denne hjemmeside." DSO sites either link to it (N1, Nord Energi Net) or do not mention a map at all. The DSOs' network development plans for 2025 (TREFOR, Vores Elnet, RAH Net) name it "Kapacitetskort". The data covers every DSO in one FeatureServer; station-to-DSO assignment is a spatial join against Energinet's DSO licence-area layer (`Netselskabsgraenser`).

Feeds F7 (substation saturation) and F9 (grid-friendliness inputs) in Denmark.

Benchmark municipalities (point-in-polygon on `Netselskabsgraenser`, 2026-10-18):

| Municipality | Zone | DSO(s) | Evidence |
|---|---|---|---|
| Ringkøbing-Skjern | DK1 | **RAH Net A/S** (Skjern, Ringkøbing, Hvide Sande, Videbæk area); also N1 A/S (east/south: Lønborghede, Ådum, Urup, Stoustrup, Kibæk, Ølgod, Nymindegab), Tarm Elværk Net and Vildbjerg Elværk (small town licence areas) | 40 stations in bbox `8.0,55.75,8.9,56.25` (a rectangle, wider than the municipality): 19 RAH, 17 N1, 2 Tarm, 2 Vildbjerg; fixture `DSO_stations_capacity_points__ringkobing_skjern_bbox.json` |
| Lolland | DK2 | **Cerius A/S** (Maribo, Rødby, Sakskøbing, Holeby, Rødbyhavn); also Nakskov Elnet A/S (Nakskov town, one station NAK) | 20 stations in bbox `10.9,54.6,11.9,55.0`: 19 Cerius, 1 Nakskov Elnet; fixture `DSO_stations_capacity_points__lolland_bbox.json` |

## Access

- Method: ArcGIS FeatureServer (public, no token) plus an Energi Data Service copy.
- Service: `https://services7.arcgis.com/an1TuZOdFp9M3PVx/arcgis/rest/services/Kapacitetskortet_V2/FeatureServer` (AGOL item `cb063f4772ab4ca9958b7c77b7ab84d5`, owner `it_gis_ENDK`, title "Kapacitetskortet_V2", snippet "Kapacity map, now including available fields", 77,220 views, last modified 2026-04-15). Native spatial reference EPSG:25832; use `outSR=4326`. `maxRecordCount` 2000 (every DSO layer is below it; page with `resultOffset` otherwise).
- Layer list (read at runtime by name; ids are positional and the service has been rebuilt before):

| id | name | geometry | rows (2026-10-18) |
|---|---|---|---|
| 0 | `TSO_stations_capacity_points` | point | 298 |
| 1 | **`DSO_stations_capacity_points`** | point | **963** |
| 2 | `TSO_stations_fields_points` | point | 186 |
| 3 | `TSO_stations_capacity_polygons` | polygon | |
| 4 | `TSO_stations_fields_polygons` | polygon | |
| 5 | **`DSO_stations_capacity_polygons`** | polygon (supply area of the station) | **936** |

- Working query (verified): `GET {service}/1/query?where=1%3D1&outFields=*&outSR=4326&orderByFields=OBJECTID&resultRecordCount=2000&f=geojson`. For `f=json` add `returnGeometry=false` to skip geometry. Spatial filter used for the benchmark fixtures: `geometry=8.0,55.75,8.9,56.25&geometryType=esriGeometryEnvelope&inSR=4326&spatialRel=esriSpatialRelIntersects`.
- Layer 1 has 963 rows but only about 937 distinct stations; 26 rows carry older `publication_date` values (see "Update dates"). Layer 5 has 936 rows.
- DSO licence areas: `https://services7.arcgis.com/an1TuZOdFp9M3PVx/arcgis/rest/services/Netselskabsgraenser/FeatureServer/0` (40 polygons). Fields: `Selsk_Nr` (int; the Forsyningstilsynet company number, e.g. 3131), `Selsk_Nvn`, `CVR`, `Areal_km2`, `Navn_kort`, `Kortnavn2`, `Website`. The geometry query for a station point is `.../0/query?geometry={lon},{lat}&geometryType=esriGeometryPoint&inSR=4326&spatialRel=esriSpatialRelIntersects&outFields=Selsk_Nvn&returnGeometry=false&f=json`. Doing the join client-side on `outSR=4326&maxAllowableOffset=0.0005` geometry worked: of 963 stations, 962 matched exactly one DSO and 1 matched none (a point outside simplified boundaries; keep it with `dso = null`).
- Energi Data Service copy: `https://api.energidataservice.dk/dataset/GridCapacityMapDSOProductionandConsumption` (id 155, P3M, filter column `publication_date`). **Stale: on 2026-10-18 it holds a single publication, `2026-01-01T00:00:00`, 933 rows, while the ArcGIS layer already carries 2026-09-01 values.** Prefer ArcGIS for current values; EDS is a lagging copy (the TSO copy is current, see the transmission dossier). It has latitude and longitude (EPSG:4326) but no DSO name, so the join is still spatial. Discontinued predecessor: `GridCapacityMapDSO` (id 157, production only; 4603 rows over publications 2024-07-01 (910 rows), 2024-10-01 (927), 2025-04-01 (916), 2025-11-01 (917), 2025-12-01 (933)). This is the only history of DSO capacity that exists: a one-off backfill for F7 trends, with different columns (no consumption, no limited-access ranges), so it must not be merged with the new dataset's series without a codebook decision. EDS returns HTTP 429 with `Retry-After` after about 25 calls within a few minutes; keep to one call per run.
- Authentication: none. Rate limits: ArcGIS did not throttle us (about 150 requests). Be polite: sequential, set the settings User-Agent.
- How it was found (no headless browser needed): the Green Power Denmark page `https://elnet.dk/` links the StoryMap; the StoryMap's item JSON `https://www.arcgis.com/sharing/rest/content/items/eb5b387e376f49b8996d5e7c47fbdd37/data?f=json` links five Experience Builder apps; `https://www.arcgis.com/sharing/rest/search?q=Kapacitetskort&f=json&num=100` lists the nine "Kapacitetskort N" web maps; each web map's `.../content/items/<id>/data?f=json` lists the layer URLs, definition expressions and renderer breaks. N1's page `https://n1.dk/tilslutningsbidrag-produktionsanlaeg` and Nord Energi Net's page `https://nordenerginet.dk/for-fagfolk/kapacitetskort/` link the same StoryMap.

### Per-DSO status

Station counts are rows in layer 1 by spatial join to `Netselskabsgraenser`. "Own viewer" = a separate BEK map on the DSO's own site (none found). The 40 licence areas are all companies in `Netselskabsgraenser`; the list matches the Forsyningstilsynet netvirksomhed list only as far as the layer goes (unverified against the Forsyningstilsynet register).

| DSO | Selsk_Nr | Stations | Map status | Own site finding |
|---|---|---|---|---|
| RAH Net A/S | 3131 | 34 | endpoint found (shared) | rahnet.dk: no map, no kapacitet page; benchmark DK1 |
| N1 A/S | 3159 | 364 | endpoint found (shared) | n1.dk page links the StoryMap; also links `https://netiifra.maps.arcgis.com/apps/instant/basic/index.html?appid=7f7fc6a0049b4e32bace92165ac7016e` (geozone map, not capacity) |
| Cerius A/S | 3143 | 109 | endpoint found (shared) | Own Power BI "Dit elnet" (netstation load vs limit, not the BEK map): embed `https://app.powerbi.com/view?r=eyJrIjoiMDgwOTU3ZjAtMTc4OC00NGM1LWFjMjgtODFkMGE2MjMwZmMzIiwidCI6IjY2YmZjYmMxLTNjYjAtNGY0Zi05ZmVhLWNjMDliYmIyN2NhZCIsImMiOjl9`; benchmark DK2 |
| Radius Elnet A/S | 3100 | 72 | endpoint found (shared) | Own Power BI "Dit elnet": embed `https://app.powerbi.com/view?r=eyJrIjoiYjBlOWU1YmQtNjk3YS00ZWY0LWFhZWUtOGM2NDVmOTQwMjhjIiwidCI6IjY2YmZjYmMxLTNjYjAtNGY0Zi05ZmVhLWNjMDliYmIyN2NhZCIsImMiOjl9` |
| Vores Elnet A/S | 3039 | 64 | endpoint found (shared) | no map on site; net development plan 2025 cites the shared tool |
| TREFOR El-net A/S | 3167 | 57 | endpoint found (shared) | plan 2025 cites the StoryMap |
| KONSTANT Net A/S | 3225 | 53 | endpoint found (shared) | no map found |
| Nord Energi Net A/S | 3033 | 45 | endpoint found (shared) | `nordenerginet.dk/for-fagfolk/kapacitetskort/` embeds the StoryMap |
| Dinel A/S | 3187 | 29 | endpoint found (shared) | no map; news on transmission capacity only |
| NOE Net A/S | 3564 | 26 | endpoint found (shared) | no map found |
| El-net Øst A/S | 3596 | 20 | endpoint found (shared) | TREFOR group site |
| Thy-Mors Energi Elnet A/S | 3111 | 20 | endpoint found (shared) | site certificate error from our client |
| EL-NET Randers A/S | 3205 | 11 | endpoint found (shared) | no map found |
| FLOW ELNET A/S | 3148 | 7 | endpoint found (shared) | no map found |
| Ravdex A/S | 3133 | 7 | endpoint found (shared) | site redirect loop |
| Elinord A/S | 3048 | 6 | endpoint found (shared) | no map found |
| Ikast El Net A/S | 3089 | 6 | endpoint found (shared) | no map found |
| L-Net A/S | 3082 | 4 | endpoint found (shared) | no map found |
| Veksel A/S | 3104 | 4 | endpoint found (shared) | no map found |
| NET8800 A/S | 3181 | 3 | endpoint found (shared) | no map found |
| Zeanet A/S | 3150 | 3 | endpoint found (shared) | news mentions Halsskov capacity only |
| NKE-Elnet A/S | 3558 | 3 | endpoint found (shared) | no map found |
| GEV Net A/S | 3058 | 2 | endpoint found (shared) | no map found |
| Forsyning Elnet A/S (Struer) | 3157 | 2 | endpoint found (shared) | no map found |
| Tarm Elværk Net A/S | 3164 | 2 | endpoint found (shared) | site 403 to our client |
| Vildbjerg Elværk A.m.b.a. | 3178 | 2 | endpoint found (shared) | site 403 to our client |
| Elektrus A/S | 3604 | 2 | endpoint found (shared) | no map found |
| Nakskov Elnet A/S | 3544 | 1 | endpoint found (shared) | Lolland benchmark (Nakskov town) |
| Læsø Elnet A/S | 3413 | 1 | endpoint found (shared) | no map found |
| Aal El-Net A.m.b.a. | 3190 | 1 | endpoint found (shared) | no map found |
| Hammel Elforsyning Net A/S | 3593 | 1 | endpoint found (shared) | no map found |
| Sunds Elforsyning | 3158 | 1 | endpoint found (shared) | no map found |
| Hjerting Transformatorforening, Midtfyns Elforsyning A.m.b.a., Aars-Hornum Net A/S, Hurup Elværk NET A/S, Videbæk Elnet A/S, Ertholmene (992) | 3077, 3135, 3195, 3199, 3177, 992 | 0 | not in the shared map | Probably no 25 to 99 kV station (unverified). Needs a human or a question to Green Power Denmark whether they are exempt or missing |
| EL-NET Hillerød A/S, EL-NET Kongerslev A/S | 3592, 3220 | 0 | not in the shared map | same as above; Kongerslev may be inside the Randers/N1 coverage |

Shared-map station counts above sum to 963 (962 matched plus 1 unmatched). Stations of a DSO that lacks its own station are served from a neighbour's 60/10 kV station, so absence is not evidence of "no capacity".

## Datasets

### D1. DSO stations (layer 1, EDS 155)

One point per DSO station (50/10 or 60/10 kV, some 150/60/10 kV combined stations). Field dictionary (ArcGIS name, EDS name, meaning; the ArcGIS names are truncated at 63 characters, hence `transf`, `transforme`, `transfo`):

| ArcGIS field | EDS field | Type | Meaning |
|---|---|---|---|
| `DSO_station_abbreviation` | `DSO_station_abbreviation` | text | Three-letter station code ("Station: Tre bogstaver, der beskriver transformerstationens navn"). Not unique across DSOs in principle; unique with `TSO_station_abbreviation` in the data seen |
| `DSO_station_name` | `DSO_station_name` | text | Station name; non-ASCII letters are transliterated in the data (`Roedbyhavn`, `Skjern syd`) |
| `TSO_station_abbreviation` | `TSO_station_abbreviation` | text | "Den nærmeste eller mest relevante transformerstation hos Energinet", e.g. `VID`, `VLO`, `ÅBØ`. Joins to the TSO layers (whose codes carry a voltage suffix, e.g. `VID_150`) |
| `voltage_level_kv` | `voltage_level_kv` | text | `60/10`, `50/10`, `150/60/10` etc.: "Det spændingsniveau, som anlægget tilsluttes" |
| `available_capacity_consumption_MW_DSO_station_without_TSO_transf` | `cap_cons_MW_no_tso` | int MW | Free consumption capacity with full network access, ignoring the TSO interface transformer (Kort 5). Rounded: 0-19 reported as 0, then steps of 10 MW (20, 30, ...) |
| `available_capacity_consumption_MW_DSO_station_with_TSO_transform` | `cap_cons_MW_with_tso` | int MW | Same, taking Energinet's transformer capacity in the shared station into account (Kort 6). Same rounding. The lower of Kort 5/6 and the transmission value is the real figure |
| `available_capacity_consumption_with_limited_grid_access_MW_lower` / `_upper` | `cap_cons_limited_MW_lb` / `_ub` | int MW | Lower and upper bound of consumption capacity with limited grid access ("begrænset netadgang", BNA); a range, not a point (e.g. 50..60) |
| `connected_consumption_with_limited_grid_access` | `conn_cons_limited_grid` | int MW | Sum of existing consumption with limited access connected at or below the station's voltage |
| `available_capacity_production_MW_DSO_station_without_TSO_transfo` | `cap_prod_MW_no_tso` | int MW | Free production capacity with full access, ignoring the TSO transformer (Kort 1). Rounded down to steps of 20 MW (0-19 -> 0, 20-39 -> 20, 40-59 -> 40 ...). Observed values: 0, 20, 40, 60, 80, 100 |
| `available_capacity_production_MW_DSO_station_with_TSO_transforme` | `cap_prod_MW_with_tso` | int MW | Same with the TSO transformer (Kort 2) |
| `available_capacity_solar_MW_lower_bound` / `_upper_bound` | `cap_solar_MW_lb` / `_ub` | int MW | Range for new solar with limited grid access, "200 hours of interruptibility". Indicative, not additive across stations |
| `available_capacity_wind_MW_lower_bound` / `_upper_bound` | `cap_wind_MW_lb` / `_ub` | int MW | Same for wind |
| `connected_solar_MW`, `connected_wind_MW` | same | int MW | Existing solar / wind capacity connected to the station (null when unknown) |
| `publication_date` | `publication_date` | epoch ms UTC (ArcGIS), ISO local string (EDS) | Publication date of the station's values |
| `ENDK_timestamp` | none | epoch ms | Energinet's load timestamp into the service; changes on reload, so ignore for change detection |
| `OBJECTID` | none | int | ArcGIS row id, not stable across reloads |
| geometry | `latitude`, `longitude` | point | ArcGIS native EPSG:25832; EDS EPSG:4326 |

Layer 5 (supply-area polygons, 936 rows) has the same attributes (except they carry raw aliases) plus `SHAPE__Area` (m2) and `SHAPE__Length` (m). Layer 5 renderer classes in the web maps are `0 - 20`, `21 - 40`, `41 - 60`, `61 - 80`, `81 - 100` MW: display bins only, not data.

**Observed quirk (2026-10-18):** in the Lolland fixture, station `NAK` (Nakskov) has `available_capacity_solar_MW_lower_bound = 55`, `..._upper_bound = 0`, `wind_lower = 45`, `wind_upper = 0` (lower greater than upper), in other rows lower is 0 and upper 25 or 20. Treat lb > ub as a publisher inconsistency: store both values as published and flag it; do not swap. On the full layer (re-counted 2026-10-18, see "Verification"): 70 rows have at least one inverted pair: 67 for solar and the same 67 for wind (always upper bound 0, lower bound above 0), and 7 for limited-access consumption (upper bound 20 or 30, never 0).

**Missing values:** `null` in a field means the DSO has not provided it (6 nulls in the limited-access lower bound, null connected solar/wind). Zero is a real published value ("0-19 MW rounds to 0"). Never merge the two.

**Update dates (layer 1):** 937 rows `publication_date` = 1788220800000 (2026-09-01T00:00Z, matching the StoryMap "Distributionsnettet: 1/9-2026 (opdateres hver 3 måned)"). Older dates remain on 26 rows: 1719792000000 (2024-07-01, 1 row), 1727740800000 (2024-10-01, 2), 1743465600000 (2025-04-01, 3), 1767225600000 (2026-01-01, 16), 1777593600000 (2026-05-01, 4). These are stations whose DSO did not republish in September; a station's freshness is its own `publication_date`, not the layer's.

**Cadence:** the StoryMap states DSO data is updated every 3 months and TSO data monthly; last DSO update 1 September 2026. Between updates the service is replaced in place (history lost), so every weekly capture matters.

### D2. Free bays and TSO stations

See `energinet-transmission-capacity.md`.

### D3. DSO licence areas (Netselskabsgraenser)

Reference geometry for the station-to-DSO join; see Access. 40 rows, EPSG:25832 native. Updated irregularly.

## How categories are encoded

**This source publishes numbers, not free/limited/none categories.** Per DSO station and per direction it publishes free MW in steps (consumption steps of 10 MW above 19; production steps of 20 MW), as separate columns for "with TSO transformer" and "without". The StoryMap itself describes colour classes (green shades for production, blue for consumption) but these are display bins in the viewer.

So:

- `capacity.free_mw.production` / `capacity.free_mw.consumption` can be filled directly from the MW columns.
- `capacity.connection_category.*` is not published by this source. Do not derive a category (for example "0 = none") in the parser. If a category is wanted, the codebook must define a documented threshold rule owned by a human; the dossier proposes none. The legal text says areas should show free/limited/no capacity, but the shared map only gives MW and the limited-access ranges.
- Limited vs. full access is encoded by column family: full access = `*_no_tso` / `*_with_tso`; limited ("flexible") access = `cap_cons_limited_MW_lb/ub`, `cap_solar/wind_MW_lb/ub` (production with limited access is split by technology). The existence of a nonzero upper bound for limited access is not a statement that a flexible connection agreement is available at that station.
- **Flexible-connection indicator (BEK §29 requires it): not published as a boolean in this source.** The only related fields are the limited-access MW ranges and `connected_consumption_with_limited_grid_access`. `capacity.flexible_connection_possible` therefore stays null from this source. Do not infer it from nonzero ranges. This is a gap for WP3.8 and a question for Energinet / Green Power Denmark (`netudvikling@energinet.dk`).
- Transmission side uses percent for 100/300 MW solar, wind and 16 consumption time bands: store as published (see transmission dossier); never convert to MW.

## Decisions

The normalisation decisions of the capacity-map loader, in one place (the asset `capacity/kapacitetskort` shows this section in Dagster, `scanner.assets.rules`; details in "Loader (WP1.5)" and "Verification of review claims"):

- **Facts, not series.** One `core.fact` per published field and station; `valid_from` is the station's own `publication_date` (UTC midnight), `known_at` the fetch time. A fact is appended only when its value differs from the latest known one, so a second run writes nothing.
- **MW as published.** Free capacity stays in MW, with the publisher's rounding (10 MW steps for consumption above 19 MW, 20 MW steps for production); no categories (free, limited, none) and no percentages are derived. Map colouring by MW needs owner thresholds.
- **Null is not zero.** A published null is stored as a JSON null and supersedes an earlier number; 0 is a real published value.
- **Inverted ranges kept.** A limited-access lower bound above its upper bound (70 stations) is stored as published, flagged in the run report, never swapped or read as "unlimited".
- **Stale rows are earlier versions.** Rows with an older `publication_date` for the same code, name and point become earlier fact versions of that station (963 rows, 937 stations on 2026-10-18); a value conflict at one publication date is skipped and reported.
- **Identity by source IDs and location.** DSO stations are `dk.substation.dso.<municipality>.<code>_<name>` (the municipality containing the point disambiguates repeated codes and names); no station is matched by name, and no DSO-to-TSO match is made without a human-approved mapping.
- **Joins only when unique.** Zone, municipality and DSO are set only for exactly one containing polygon; overlapping licence areas leave `dso` null with candidates and file a `human` bead.
- **F7's station value** is the lower of the with- and without-TSO-transformer MW, as the StoryMap says ("den laveste værdi angiver den faktiske ledige kapacitet"); a feature-level choice in `scanner.features.substation`, not a stored value. Limited-access ranges are no F7 input.

## Licence and terms

- ArcGIS items: `licenseInfo` and `termsOfUse` are empty on `Kapacitetskortet_V2`. No attribution requirement stated.
- StoryMap disclaimer (verbatim): "Kapacitetskortet må på ingen måde tolkes som en forpligtigelse eller et tilsagn fra hverken netselskaberne eller Energinet ... Kapacitetskortet viser et øjebliksbillede." Carry this wording or an equivalent wherever the data is shown.
- StoryMap section "Hent data" says: "Data bag alle kortene kan hentes via Energi Data Service", with data questions to `netudvikling@energinet.dk`. That is the nearest thing to a redistribution statement; the EDS terms govern (tracked by `dk.eds.terms`, owned elsewhere). Redistribution in a paid product is not stated: unverified, ask Energinet.
- Terms to watch: the StoryMap text and the EDS terms page.

## Measured or estimated

Calculated, indicative values from the DSOs' load-flow calculations using a shared simplified method (N-0 for production with full access, N-1 for consumption with full access, N-0 with 200 interruptible hours a year for limited-access production; criteria quoted in the StoryMap: existing plus agreed connections, limited-access plants excluded for the full-access value). Each station is computed independently; "den ledige kapacitet, der er opgjort pr. station, kan således ikke summeres". Values are rounded by the publisher (10 MW and 20 MW steps), which makes them coarse. They are not measurements.

## Sensitivity

`public`.

## Codebook entries needed

Proposed file: `codebook/dk/kapacitetskort.dso_stations.yaml` (source short `kapacitetskort` for the ArcGIS route; the same fields exist in EDS as `eds.GridCapacityMapDSOProductionandConsumption` and should be one set of entries with an alias, not duplicated). Dimension `access` is `full` or `limited`; dimension `tso_transformer` is `with` or `without`. All entries: kind `capacity`, resample `none`, sensitivity `public`, equivalence-class assignment pending owner sign-off.

| Source field (ArcGIS / EDS) | Proposed id suffix | Equivalence class | Unit | Notes |
|---|---|---|---|---|
| `..._consumption_MW_DSO_station_without_TSO_transf` / `cap_cons_MW_no_tso` | `cap_cons_full_no_tso` | `capacity.free_mw.consumption` | MW | Dimensions access=full, tso_transformer=without. Published in 10 MW steps |
| `..._consumption_MW_DSO_station_with_TSO_transform` / `cap_cons_MW_with_tso` | `cap_cons_full_with_tso` | `capacity.free_mw.consumption` | MW | tso_transformer=with. The F7 default should be the minimum of `with` and `without` only after a human decision; the source says the lowest value across maps is the real one |
| `..._production_MW_DSO_station_without_TSO_transfo` / `cap_prod_MW_no_tso` | `cap_prod_full_no_tso` | `capacity.free_mw.production` | MW | 20 MW steps |
| `..._production_MW_DSO_station_with_TSO_transforme` / `cap_prod_MW_with_tso` | `cap_prod_full_with_tso` | `capacity.free_mw.production` | MW | |
| `..._consumption_with_limited_grid_access_MW_lower` / `_upper` | `cap_cons_limited_lb`, `cap_cons_limited_ub` | `capacity.free_mw.consumption` with dimension access=limited, bound=lower/upper | MW | Proposes the class is reused with a dimension rather than a new class; the owner may prefer new classes `capacity.free_mw_limited.consumption`. Keep bounds as two series |
| `..._solar_MW_lower_bound` / `_upper_bound`, `..._wind_MW_...` | `cap_solar_limited_lb`, `..._ub`, `cap_wind_limited_lb`, `..._ub` | `capacity.free_mw.production` with access=limited, technology=solar/wind | MW | Same remark. Observed lb > ub in rows: keep both, flag |
| `connected_solar_MW`, `connected_wind_MW`, `connected_consumption_with_limited_grid_access` | `connected_solar`, `connected_wind`, `connected_cons_limited` | none (new class proposal `capacity.connected_mw` with technology dimension) | MW | Existing connected capacity, not free capacity |
| `DSO_station_abbreviation`, `DSO_station_name`, `TSO_station_abbreviation`, `voltage_level_kv`, geometry | not series | entity attributes | | Create `ref.entity` per station: proposed id `dk.substation.<tso_abbr>_<dso_abbr>` normalised to `[a-z0-9_.-]` (transliterate å/ø/æ and lower-case); parent = DSO entity |
| `publication_date` | valid_from of the fact | | | Valid from this date until the station's next publication |

Not ingested (no class, no reliable meaning): `ENDK_timestamp`, `OBJECTID`, `SHAPE__*`.

Not available from this source (leave null, file a `human` bead): `capacity.connection_category.consumption`, `capacity.connection_category.production`, `capacity.flexible_connection_possible`.

Transmission-side fields: see `energinet-transmission-capacity.md`. Proposed new classes for percent-denominated transmission fields: `capacity.deliverable_share_pct.solar` / `.wind` (100 MW and 300 MW test sizes as a dimension) and `capacity.servable_share_pct.consumption` (time band and season as dimensions), and `capacity.free_bay` (categorical Ja / Nej / Ikke afdækket). These do not exist yet and must not be mapped into the `free_mw` classes.

## Risks

- The service is a rebuilt "V2" with positional layer ids; discovered by name at runtime. Layer renames or a V3 are likely (TSO part is announced to change in Q4 2026).
- Single point of failure: one service run by Energinet's GIS team; no versioning, no history. Weekly snapshots are the mitigation.
- Rounded values (10/20 MW steps) limit F7 resolution; a station at 0 can still have 19 MW free.
- DSO sites we could not read (certificate error, 403, redirect loop): Konstant, Thy-Mors, Midtfyns, Ravdex, Tarm, Vildbjerg. These do not affect the shared map but mean we cannot rule out an additional own viewer there.
- 11 licence areas have no station in the shared layer; whether that is lawful (under 25 kV only) or an omission is unverified.
- Legal text may be amended; `BEK 1468` §29 is the basis, reread when it changes.

## Loader (WP1.5)

`scanner.runtime.capacity_map.load_capacity_map` (CLI `capacity-map`, Dagster asset `capacity/kapacitetskort`, weekly Monday 06:30 Europe/Copenhagen) loads layers `DSO_stations_capacity_points`, `TSO_stations_capacity_points` and `TSO_stations_fields_points` by name, plus `Netselskabsgraenser`, as GeoJSON in EPSG:4326, paging past `maxRecordCount`. Every response goes to the raw store and `core.source_document`; each run is one `ops.ingest_run` row (source `kapacitetskort`, dataset `Kapacitetskortet_V2`). Parsing rules: `scanner.adapters.kapacitetskort.stations`. Codebook: `codebook/dk/kapacitetskort.*.yaml`.

- **Entity IDs.** TSO stations: `dk.substation.energinet.<slug(code)>` (`MAL_150` -> `mal_150`), shared by the capacity and free-bay layers when the code is identical. DSO stations: `dk.substation.dso.<municipality>.<slug(code)>_<slug(name)>` (`dk.substation.dso.aarhus.has_hasle`). Why: (code, name) is not unique (`HAS Hasle` exists in Aarhus and on Bornholm, `SBY Saeby` in Jutland and on Zealand), the TSO reference changes between publications, and the licence areas overlap, so the DAGI municipality containing the point disambiguates (`unlocated` if none). The slug folds æ/ø/å to ae/oe/aa and keeps `$` as `s`: `BRA_150--$` ("Hedensted", capacity layer) and `BRA_150` ("Bredal", free-bay layer, 2.5 km away) stay separate entities pending a human match. DSO licence areas are `dk.dso_area.<slug(Navn_kort)>` with their polygons.
- **Stale rows.** The 26 DSO rows with older `publication_date` have the same code, name and point as a current row (the station was republished under a new TSO reference without the old row being removed). They become earlier fact versions of the same entity (963 rows -> 937 stations on 2026-10-18).
- **Joins** (`st_covers` against `ref.entity_geometry`): `parent_id` is the zone if the point is in exactly one zone polygon, else the country; `attributes.municipality` and `attributes.dso` are set only for exactly one match. 36 stations lie in two overlapping licence areas (at full resolution the larger polygons have no holes: RAH, Ikast, Vildbjerg, GEV, Sunds, NET8800 and EL-NET Randers inside N1; Hurup inside Thy-Mors; L-Net inside NOE; Zeanet inside Cerius; Hillerød inside Radius; Midtfyns inside Vores Elnet): `dso` stays null with `dso_candidates`, and the run files one `human` bead asking for a rule (for example the smaller, enclosed area). The per-DSO station counts above came from a coarser join and are not confirmed by this one. `MAV Masnedoe` (coordinates about 1 degree off) and the free-bay station `ASV_132` (on the coast) match no zone or municipality.
- **Facts.** One `core.fact` per published field; `valid_from` = the station's `publication_date` (UTC midnight), `known_at` = fetch time, `attribute` = codebook entry ID. A published null is stored as a JSON null in `value_json`, so it stays distinct from 0 and supersedes an earlier number. A fact is appended only when its value differs from the latest known one for (entity, attribute) at that valid time; a second run writes nothing. Entities a run no longer finds get `attributes.listed = false`.
- **Not derived:** no categories and no percentages. F7 (`scanner.features.substation`) takes the lower of the with- and without-TSO-transformer values as the station's free MW, per the StoryMap ("Sammenhold kapaciteten fra Kort 1, 2 og 3 – den laveste værdi angiver den faktiske ledige kapacitet"); the transmission value is not applied because the DSO-to-TSO station link is not an approved match. Map colouring by MW needs owner thresholds (`assumptions/saturation_thresholds_v0.yaml` covers percentages and categories only).
- **F7 version 2 (2026-10-18)** adds per-direction station counts, mean free MW per station (the MW-weighted variant: the map publishes no station capacity, so free MW over capacity cannot be formed), TSO-inclusive variants and direction-aware statistics (stations with free capacity in one direction and none in the other); `docs/methods/f7_substation_saturation.md`. Transmission consumption figures are treated as not assessed (inferred, see `energinet-transmission-capacity.md`). Limited-access ranges are no F7 input: the 70 inverted ranges stay as published and flagged, and no statistic reads an upper bound of 0 as 0 MW or as unlimited (inferred handling, question 2 of `docs/questions/capacity-map-semantics.md`).

## Verification of review claims (2026-10-18)

Method: full layers 0, 1, 2 fetched live (`returnGeometry=false`), the StoryMap item JSON (`https://www.arcgis.com/sharing/rest/content/items/eb5b387e376f49b8996d5e7c47fbdd37/data?f=json`), the EDS metadata (`https://api.energidataservice.dk/meta/dataset/GridCapacityMapDSOProductionandConsumption` and the TSO twin) and the main DB (read-only). Open questions: `docs/questions/capacity-map-semantics.md`.

### 963 rows, 937 stations (what the loader does with the extra rows)

Layer 1 has 963 rows; 937 carry the current `publication_date` 2026-09-01 and 26 carry older dates (2024-07-01: 1, 2024-10-01: 2, 2025-04-01: 3, 2026-01-01: 16, 2026-05-01: 4). The loader (`scanner.adapters.kapacitetskort.stations.build_stations`) groups rows by entity ID (`dk.substation.dso.<municipality>.<code>_<name>`); a stale row has the same code, name and point as a current row, so it lands on the same entity (937 entities, confirmed in `ref.entity`). Per entity:

- name, point and attributes come from the row with the latest `publication_date`;
- every row, current or stale, produces candidate facts with `valid_from` = that row's `publication_date`; a candidate is written only if its value differs from the latest known value at that valid time (`db.write_facts`). So a stale row adds an earlier fact version only when its numbers differ from what is already known, and never overrides the current row. The per-attribute fact counts (950 to 955 for the DSO fields, against 937 stations) are these extra versions. No row is dropped silently and no row is deduplicated by guessing;
- two rows of one entity with the same publication date and different values are a value conflict: skipped and listed in the run report.

Consequence: "937 stations" is the number of DSO entities; the other 26 rows are older versions of the same stations. A station's freshness is its own latest `publication_date`.

### Caveat to display on site cards

Source: the StoryMap (`https://storymaps.arcgis.com/stories/eb5b387e376f49b8996d5e7c47fbdd37`, authors Energinet and Green Power Denmark; kapacitetskort.dk redirects there). Verbatim, in the order to use:

- "Kapacitetskortet er et værktøj, der giver et vejledende estimat for den ledige kapacitet ..." (a guiding estimate).
- "Kapacitetskortet må på ingen måde tolkes som en forpligtigelse eller et tilsagn fra hverken netselskaberne eller Energinet. Før enhver tilslutning skal netselskabet og/eller Energinet udføre beregninger for den enkelte tilslutning. Kapacitetskortet viser et øjebliksbillede. Allerede dagen efter en opdatering kan der være indgået aftaler om nye tilslutninger, som kan betyde, at den ledige kapacitet i det pågældende område ikke længere er tilgængelig."
- "Den ledige kapacitet er opgjort under de forudsætninger, der var aktuelle på beregningstidspunktet, og det er vejledende størrelser til brug i den videre dialog og planlægning. Den præcise ledige kapacitet opgøres først efter indgået aftale med enten et netselskab eller Energinet."
- "Beregningerne er indikative og kan ikke blive adderet på tværs af stationer."

Suggested card text (our translation, mark as such): "Indicative estimate from the national capacity map (Energinet and Green Power Denmark), as of <publication_date>. Not an obligation or a commitment from the grid companies or Energinet; the exact capacity is only determined after an agreement with the grid company or Energinet. Station values cannot be added up."

Served (migration `20261018140000_f7_site_cards`, view `feat.capacity_map_caveat`) as `capacity_map_caveat` on `serving.ranked_sites` and `serving.substation_map`: the second and fourth quotes above, verbatim; and `capacity_map_caveat_en`: the suggested card text without the date, ending "(Our translation.)". The English EDS description also says "Values are indicative and the station values cannot be summarized." There is no separate Energinet-page wording: `https://www.energinet.dk/el/eltransmissionsnettet/kapacitetskort/` is a thin page pointing at the StoryMap (checked 2026-10-18).

### Inverted limited-access ranges (lower bound above upper bound)

Counted on the live layer: 70 stations (62 in the DB are Cerius 34 and Radius 28; the rest NKE 3, Elektrus 1, Zeanet 1, Nakskov 1, 2 with no unique DSO). Detail: solar 67 stations with lower > upper and upper = 0; wind the same 67; consumption (limited access) 7 stations with lower > upper, upper 20 or 30. Elsewhere in the layer lower = upper for 187 stations (solar and wind each) and null in 7 (consumption 6).

Documentation: the field has no definition of the range or of upper = 0. The EDS column text says only "Lower bound of available capacity in MW for connection and integration of new solar production at limited grid access with 200 hours of interruptibility. Values are indicative and the station values cannot be summarized." (`cap_solar_MW_lb`, upper bound analogous; consumption: "The calculated lower MW value for connecting consumption" / "The calculated upper MW value"). The StoryMap explains limited access ("Kapaciteten for begrænset netadgang, der er angivet, er beregnet med 200 timers årlig afbrydelighed for hhv. sol- og vindproduktion"; "Kapacitet for sol- og vindproduktion med begrænset netadgang beregnes i N-0 200 timers årlig afbrydelighed") but nowhere says what lower and upper mean, or that upper = 0 means "no limit" or "not calculated". Undocumented.

Inferred (not documented): upper = 0 together with a positive lower bound is a sentinel, not a range, because it is the single pattern for all 67 solar/wind cases and the lower bound is then a plausible MW value. Whether it means "no upper limit", "not calculated" or a data error cannot be decided from the data. Handling: store both values as published (already done, 70 rows flagged as quirks), do not swap, do not read upper = 0 as "zero capacity" nor as "unlimited". The consumption inversions (upper 20 or 30) are a different pattern and stay unexplained. Question 2 in the questions file.

### Station names repeat across the DSO and TSO layers

Distinct station names: 930 (layer 1, lower-cased) and 179 (layers 0 and 2). Names in common: 95 exactly, 96 lower-cased (99 DSO rows); the DB entities give 95 / 97. The review's 119 does not reproduce with these definitions (it may count rows or a looser match; unverified). Either way the conclusion holds: names are not identifiers (`HAS Hasle` exists in Aarhus and on Bornholm, see Loader). Code audit: no code path joins or matches stations by name. `capacity_map.py` and `stations.py` use names only to fill `ref.entity.name` and, together with the code, in the DSO entity ID (a composite key because the source has no unique ID; it is never compared across layers); `scanner.features.*` read facts by entity ID; the serving views select `e.name` for display only. The DSO-to-TSO link (`tso_station_abbreviation`, bare code such as `VID` against suffixed TSO codes such as `VID_150`) is stored as an attribute and not used for any join. Rule: all matching uses source IDs; a DSO-TSO match needs a human-approved mapping.

### Update cadence

- StoryMap banner: "Transmissionsnettet: 1/2-2026 (opdateres hver måned)" and "Distributionsnettet: 1/9-2026 (opdateres hver 3 måned)".
- StoryMap background text: "Kapacitetskortet opdateres kvartalsvis af netselskaberne og månedligt af Energinet. Det betyder, at der lokalt både kan være mere ledig kapacitet end angivet, (hvis elnettet er blevet forstærket siden den seneste opdatering) eller mindre ledig kapacitet end angivet (hvis der er blevet aftalt tilslutning af et eller flere nye produktionsanlæg siden den seneste opdatering)."
- BEK 1468 §29 (as summarised above): at least every three months.
- EDS: DSO dataset `P3M`, TSO dataset `P1M`.
- Observed: DSO publication dates are the first day of a quarter-ish month (2024-07-01, 2024-10-01, 2025-04-01, 2026-01-01, 2026-05-01, 2026-09-01); stations republish individually (26 stale rows). TSO data has been 2026-02-01 since February, against the monthly promise.

Recommendation (inferred): keep the weekly snapshot. Values change at most quarterly (DSO) and the loader writes nothing when values are unchanged, so weekly costs almost nothing and catches individual stations republished between quarters, the announced TSO refresh and a service rebuild. A change in the layer list or field names fails the run loudly.

## Snapshots

Every map and endpoint is in `registry/snapshots.yaml` from day one, because the maps are replaced in place each quarter and their history is lost otherwise: ids `dk.capacity_map.*`, `dk.dso.capacity_map.*`, and one `dk.dso.<name>.capacity_map_page` entry per DSO (those with no own viewer are `to_research`, with the finding in the note). The ArcGIS and EDS snapshots are the real captures; the per-DSO entries only matter if a DSO ever publishes its own viewer.
