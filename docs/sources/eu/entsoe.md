# Source dossier: ENTSO-E Transparency Platform

Status: draft, verified against public documentation on 2026-10-18 by the entsoe agent (WP3.1 phase A); awaiting owner approval. Country: eu (base tier for every country). Owner: Misi. Last verified: 2026-10-18.

Facts marked **unverified** could not be checked without an API token. The fixture recording of 2026-10-18 (`scripts/record_entsoe_fixtures.py`, 14 cases, `tests/fixtures/entsoe/`) settled these:

- Header authentication (`SECURITY_TOKEN` on GET) works: 14 requests, 14 HTTP 200.
- Every response used curveType A03; DK1 prices requested with `curveType=A01` decode to the same 96 records.
- DK1 day-ahead: one TimeSeries per day without `classificationSequence`, equal to Energi Data Service `DayAheadPrices` within 1.3e-5 EUR/MWh; PT60M on 2025-09-30, PT15M on 2025-10-01 (two TimeSeries in one response); 100 quarter-hours on 2025-10-26, 92 on 2026-03-29. On 2026-03-29 a second TimeSeries with sequence 2 repeats the same prices.
- DE-LU day-ahead: sequence 1 equals Energi Data Service DE (SDAC); sequence 2 differs by up to 42 EUR/MWh (another auction). Ingest rule: sequence 1, or no sequence, is the SDAC price.
- NL day-ahead: one TimeSeries without a sequence, PT15M.
- DK1 actual total load (A65): PT60M with 9 of 24 hours of 2026-10-14 missing (separate Periods); gaps stay gaps.
- DK1 generation per type (A75): PT15M, eight production types, no consumption series, no negative values; solar reads about 29 MW at night, as published.
- DK1 -> DE-LU flows (A11) are published under the bidding-zone codes (PT15M); the DE(TenneT GER) control-area query returns "No matching data found". Offered (A31) and forecasted (A61) capacity DK1 -> DE-LU exist at PT15M.
- A future day returns HTTP 200 with an Acknowledgement, reason 999 "No matching data found for Data item ENERGY_PRICES [12.1.D] ...".

## What it is

The pan-European publication platform for market and system data required by Regulation (EU) 543/2013 (the Transparency Regulation, "TR"), the Electricity Balancing guideline (EU 2017/2195) and the System Operation guideline (EU 2017/1485). Every bidding zone publishes the same document types, which makes it the base tier of the country-agnostic model (ADR 0011): a country covered by ENTSO-E needs only `registry/countries/<cc>.yaml`.

Used by: F1 (day-ahead prices, reference zones), F2 and F4 (generation, load) for countries without national sources, F3 (physical flows, transfer capacity), later F11 (balancing).

## Sources consulted

The old static user guide (`transparency.entsoe.eu/content/static_content/Static content/web api/Guide.html` and its 2024 backup) no longer loads: on 2026-10-18 it returns HTTP 400 `URI_FORMAT_ERROR` from the new platform. Its replacements:

- Official Postman collection "Transparency Platform Restful API", published 2026-01-16: <https://documenter.getpostman.com/view/7009892/2s93JtP3F6> (raw: <https://documenter.gw.postman.com/api/collections/7009892/2s93JtP3F6>).
- Knowledge base, section Web API: <https://transparencyplatform.zendesk.com/hc/en-us/sections/12783116987028-Web-API> (individual articles cited below).
- Manual of Procedures v3r5 (<https://www.entsoe.eu/data/transparency-platform/mop/>), zip <https://eepublicdownloads.blob.core.windows.net/public-cdn-container/clean-documents/mc-documents/transparency-platform/MOP/MoP_v3r5_final.zip>: Ref2 "Detailed Data Descriptions" v3r5 (2025-11-14, "DDD"), Ref19 "Data extraction process IG" v1r5, Ref5 "Generation and Load IG" v5r0.
- XSDs, ENTSO-E transparency XSD repository: `iec62325-451-3-publication_v7_3.xsd`, `iec62325-451-6-generationload_v3_0.xsd`, `iec62325-451-1-acknowledgement_v7_0.xsd` at <https://gitlab.entsoe.eu/transparency/xsd/-/tree/main>.
- "The introduction of different time series possibilities (curvetype) within ENTSO-E electronic documents" v1.4 (2022-02-01): <https://eepublicdownloads.entsoe.eu/clean-documents/EDI/Library/cim_based/Introduction_of_different_Timeseries_possibilities__curvetypes__with_ENTSO-E_electronic_document_v1.4.pdf>.

## Access

- Endpoint: `https://web-api.tp.entsoe.eu/api` (test: `https://web-api.tp-iop.entsoe.eu/api`), HTTPS only. [Request Endpoint](https://transparencyplatform.zendesk.com/hc/en-us/articles/15696677194644-Request-Endpoint)
- Methods: GET with query parameters, or POST with a `StatusRequest_MarketDocument` XML body. [Request Methods](https://transparencyplatform.zendesk.com/hc/en-us/articles/15696643163924-Request-Methods)
- Response: XML (IEC 62325-451 documents); some articles return zip archives (see "Errors and special responses").

### Authentication: header, not query parameter

A personal security token (a UUID) is required; missing or invalid tokens get HTTP 401 ([Authentication and Authorization](https://transparencyplatform.zendesk.com/hc/en-us/articles/15854491895828-Authentication-and-Authorization)). The token is requested by e-mail to transparency@entsoe.eu and generated under "My Account" ([How to get security token](https://transparencyplatform.zendesk.com/hc/en-us/articles/12845911031188-How-to-get-security-token)). Runtime secret: `ENTSOE_API_TOKEN` (alias `ENTSOE_API_KEY`), `scanner.settings.Settings.entsoe_api_token`.

Two documented ways to send it:

- GET: query parameter `securityToken` ([Get Method](https://transparencyplatform.zendesk.com/hc/en-us/articles/15854521447188-Get-Method)).
- HTTP header `SECURITY_TOKEN`. Documented for POST ([Post Method](https://transparencyplatform.zendesk.com/hc/en-us/articles/15854497223188-Post-Method)) **and for GET**: the official Postman collection contains the request "6.1.A Actual Total Load - SECURITY_TOKEN in header" (<https://documenter.getpostman.com/view/7009892/2s93JtP3F6#f6b9b190-2b87-4cd1-9409-0c4f39399881>), a GET with header `SECURITY_TOKEN: {{apiKey}}`, no query token, and the note "This is an example where the security token is included in the request header rather than in the query parameters."

Decision: the scanner sends the token only in the `SECURITY_TOKEN` header (`scanner.adapters.entsoe.client.SecurityTokenAuth`). URLs then never contain it, so httpx's INFO log line (`HTTP Request: GET <url>`) and httpx exception messages, which quote the URL, cannot leak it. Second line of defence in `client.py`: `redact`, `redact_params`, `stored_headers`, and `get_document` refuses to return a FetchResult in which the token still appears (raw and URL-encoded forms checked); errors are re-raised as `EntsoeHttpError` with a redacted message and no exception chain. Whether header authentication works with our token is **unverified** until the first recording; the recorder stops at the first 401.

### Time parameters and query limits

- `periodStart`, `periodEnd`: `yyyyMMddHHmm`, UTC, GET only; alternatively `timeInterval=yyyy-MM-ddTHH:mmZ/yyyy-MM-ddTHH:mmZ` (GET and POST). [Request Parameters - Time Interval](https://transparencyplatform.zendesk.com/hc/en-us/articles/12783280128404-Request-Parameters-Time-Interval)
- Parameter names are case sensitive; omitting an optional parameter requests all its values. [Request Parameters](https://transparencyplatform.zendesk.com/hc/en-us/articles/15696716612372-Request-Parameters). The Postman collection spells the contract parameter `contract_MarketAgreement.type` for 12.1.D and `contract_MarketAgreement.Type` for 11.1; the recorder follows each spelling.
- Maximum span: one year for the articles we use (6.1.A, 12.1.D, 12.1.G, 16.1.B&C, 11.1.A forecasted); offered capacity (11.1.A) is limited to 100 documents. [API Query Size Limit](https://transparencyplatform.zendesk.com/hc/en-us/articles/15854536354964-API-Query-Size-Limit)
- 12.1.D responses hold at most 100 TimeSeries; `offset` pages through them (Postman, "12.1.D Energy Prices").
- Daily articles (prices, offered capacities, among others) return whole days: a query touching two days returns both. Border queries (`in_Domain`, `out_Domain`) return one direction; swap the codes for the other. [Query Response](https://transparencyplatform.zendesk.com/hc/en-us/articles/15727773247124-Query-Response)
- Request timeout: 300 seconds. [API Rate Limit Part 2](https://transparencyplatform.zendesk.com/hc/en-us/articles/12783223209876-API-Rate-Limit-Part-2)

### Rate limits

400 requests per minute per user account (token), counted per token since the R3 platform (no IP-based bans); exceeding it gives HTTP 429 and a temporary ban of about 10 minutes; recommended client throttle 6 to 7 requests per second ([API Rate Limit Part 1](https://transparencyplatform.zendesk.com/hc/en-us/articles/12783148966036-API-Rate-Limit-Part-1), updated 2026-10-08). The older Query Response article still says "per unique IP"; the newer article wins. Our runs are sequential with a few requests per partition, far below the limit; the recorder pauses 2 s between requests and stops on 429.

### Errors and special responses

| HTTP | Reason code | Meaning |
|---|---|---|
| 200 | 999 | No matching data found |
| 400 | 999 | Invalid query attributes or parameters |
| 401 | | Missing or invalid security token |
| 429 | | Too many requests |

Source: [Query Response](https://transparencyplatform.zendesk.com/hc/en-us/articles/15727773247124-Query-Response). The error body is an `Acknowledgement_MarketDocument` with one or more `Reason` (`code`, `text`) elements; rejection causes include missing mandatory or duplicate parameters, wrong letter case, no data, a range over one year and more than 100 matching documents ([Rejection of Request](https://transparencyplatform.zendesk.com/hc/en-us/articles/15854332833300-Rejection-of-Request), which also shows a sample acknowledgement used in our tests). Because code 999 covers both "no data" and "invalid query", the HTTP status tells them apart; `AcknowledgementError.no_matching_data` checks the text.

Zipped responses: outages (A77, A78, A80) and flow-based allocations return zip archives (Postman example responses, `Content-Disposition: attachment; filename="...zip"`, content type `application/zip`, `application/octet-stream` or even `application/xml`). None of the datasets below do; the decoder rejects a zip payload with a ParseError rather than guessing, and zip support arrives with the first dataset that needs it.

## Datasets

Document, process and parameter codes from the Postman collection and the knowledge-base code lists ([DocumentType](https://transparencyplatform.zendesk.com/hc/en-us/articles/15857043092756-DocumentType), [ProcessType](https://transparencyplatform.zendesk.com/hc/en-us/articles/15857039772052-ProcessType), [BusinessType](https://transparencyplatform.zendesk.com/hc/en-us/articles/15857010932500-BusinessType), [PsrType](https://transparencyplatform.zendesk.com/hc/en-us/articles/15856995130004-PsrType), [Contract_MarketAgreement.Type](https://transparencyplatform.zendesk.com/hc/en-us/articles/15856778598548-Contract-MarketAgreement-Type)). Definitions quoted in the codebook files come from the DDD v3r5.

| Our dataset | TR article | Request | Response root | Value | Unit |
|---|---|---|---|---|---|
| EnergyPrices | 12.1.D | `documentType=A44`, `in_Domain` = `out_Domain` = zone EIC, optional `contract_MarketAgreement.type=A01` (day-ahead; A07 intraday) | Publication_MarketDocument | `price.amount` | `currency_Unit.name` EUR, `price_Measure_Unit.name` MWH |
| ActualTotalLoad | 6.1.A | `documentType=A65`, `processType=A16` (realised), `outBiddingZone_Domain` | GL_MarketDocument | `quantity` | MAW (MW) |
| ActualGenerationPerType | 16.1.B&C | `documentType=A75`, `processType=A16`, `in_Domain`, optional `psrType` | GL_MarketDocument | `quantity` | MAW |
| PhysicalFlows | 12.1.G | `documentType=A11`, `out_Domain` (from), `in_Domain` (to) | Publication_MarketDocument | `quantity` | MAW |
| OfferedTransferCapacity | 11.1 | `documentType=A31`, `auction.Type=A01` (implicit), `contract_MarketAgreement.Type=A01` (day-ahead), `out_Domain`, `in_Domain` | Publication_MarketDocument | `quantity` | MAW |
| (alternative) Forecasted capacity | 11.1.A | `documentType=A61`, `contract_MarketAgreement.Type=A01` (A02 week, A03 month, A04 year) | Publication_MarketDocument | `quantity` | MAW |

### Day-ahead prices (A44)

One TimeSeries per delivery day (or more). Some zones publish several TimeSeries per day distinguished by `classificationSequence_AttributeInstanceComponent.position` (the official AT example for 2024-07-28 carries position 2 at PT15M); which position is the SDAC price for each zone is **unverified** and must be read from recorded fixtures before the codebook entry is approved. Resolution follows the market time unit: SDAC moved to 15 minutes on trading day 2025-09-30 for delivery day 2025-10-01 in all coupled zones ([OTE, MCSC confirmation](https://www.ote-cr.cz/en/about-ote/ote-news/market-coupling-steering-committee-confirms-go-live-of-15-minute-mtu-in-sdac-on-trading-day-30-september-2025-for-delivery-day-1-october-2025); [EPEX SPOT](https://www.epexspot.com/en/news/successful-implementation-15-minute-market-time-unit-mtu-sdac)), matching `market_rules/dk.yaml` `day_ahead.resolution` (Ireland's SEM uses 30 minutes). A query covering 2025-09-30 and 2025-10-01 should therefore return PT60M then PT15M Periods for DK1 (recorded as a fixture). Response days are normally the area's local day, but the [Response Time Zone](https://transparencyplatform.zendesk.com/hc/en-us/articles/12786431986964-Response-Time-Zone) article names 12.1.D as an exception "due to regional arrangements"; the adapter filters records by UTC interval, so it does not depend on this.

### Actual total load (A65/A16)

DDD 6.1.A: total load = net generation minus exports plus imports minus power absorbed by storage, losses included, average of real-time values per MTU, net generation estimated where unknown. Published at the latest one hour after the operating period. Resolution varies by zone (the CZ example is PT60M); DK1 is **unverified**. Not equivalent to Energi Data Service `GrossCon` without an owner decision; see `codebook/eu/entsoe.ActualTotalLoad.yaml`.

### Actual generation per production type (A75/A16)

DDD 16.1.B&C: average net generation output (MW) per MTU and production type; small units may be estimated. A74 returns the same data (Postman note). "Time series with inBiddingZone_Domain attribute reflects Generation values while outBiddingZone_Domain reflects Consumption values" (Postman note); the official DE example has consumption series for B10 (hydro pumped storage), B16 (solar) and B19 (wind onshore). Only generation series are ingested. Production types: B16 solar, B18 wind offshore, B19 wind onshore, B25 energy storage, others in the PsrType list.

### Cross-border physical flows (A11)

DDD 12.1.G: measured power between neighbouring bidding zones, average netted values per MTU, DC links measured at the sending end unless stated. The API answers per direction and, unlike the website, does not net the two directions (Postman note). Borders may be published under control-area codes (the official example uses DE(Amprion)); for DK1 to Germany both the DE-LU bidding zone and DE(TenneT GER) control area (`10YDE-EON------1`) are recorded.

### Transfer capacities (A31, A61)

The DDD (11.1.A) says forecasted capacities are not published where flow-based allocation applies; offered NTC capacity likewise exists only on NTC borders, flow-based regions publish flow-based parameters (B11, zipped) instead. Offered capacity can be negative between Norwegian zones. Which Danish borders still have NTC values since the Nordic flow-based go-live is **unverified**; DK1 to DE-LU is recorded with both A31 and A61.

### Balancing (later)

Imbalance prices (17.1.G, A85, `controlArea_Domain`), prices of activated balancing energy (17.1.F, A84), contracted reserves (17.1.B&C) and the GL EB items (12.3.x) are in the Postman collection's Balancing folder. They return `Balancing_MarketDocument`, which the decoder does not support yet (Points carry several value elements). Phase 3 or later.

## Time convention

All request and response times are UTC ([Request Parameters - Time Interval](https://transparencyplatform.zendesk.com/hc/en-us/articles/12783280128404-Request-Parameters-Time-Interval), [Query Response](https://transparencyplatform.zendesk.com/hc/en-us/articles/15727773247124-Query-Response)). Each TimeSeries has one or more `Period`s, each with its own `timeInterval` (`start`, `end`, e.g. `2024-07-27T22:00Z`) and `resolution` (ISO 8601: PT15M, PT30M, PT60M, P1D, ...). Point `position` p covers `[start + (p - 1) * resolution, start + p * resolution)` (curvetype document, section 4). Intervals are half-open and in UTC, so DST needs no special handling: a Copenhagen day is 92, 96 or 100 quarter-hours simply because its UTC span is 23, 24 or 25 hours. Resolutions differ by zone and date; store native (ADR 0002).

## Decoding (for human sign-off)

Implemented in `src/scanner/adapters/entsoe/decode.py`, tested in `tests/test_entsoe_decode.py`.

- **curveType A01**, sequential fixed-size blocks: every position of the Period is present ("All Intervals to cover the TimeInterval of a Period must be present"; A01 is the default when `curveType` is absent). Curvetype document, sections 4 and 4.1.
- **curveType A03**, variable-sized blocks: "only the position where a block change occurs is provided"; each block runs to the start of the next one, the last to the end of the Period (curvetype document, section 4.3). The knowledge base states it plainly: "In case a point is missing, it means that it has the same value as the previous point" ([CurveType=A01 vs CurveType=A03](https://transparencyplatform.zendesk.com/hc/en-us/articles/30262342482961-CurveType-A01-vs-CurveType-A03), updated 2026-09-23). Responses use A03 by default ([Query Response](https://transparencyplatform.zendesk.com/hc/en-us/articles/15727773247124-Query-Response): "Curve type in response is A03 for all data items"), and every request may set `curveType=A01` to get fixed blocks (Postman, optional parameter on each request).
- **Our rule**: an A03 block is expanded into one record per position, each with the block's value and `source_position` pointing at the Point that carried it. This decodes the format: the source states that value for every position of the block. It is not forward-filling, which would invent values for positions the source left undefined. **This needs owner sign-off** (AGENTS.md rule 2). The recording includes DK1 prices for the same day with and without `curveType=A01`, so phase B can prove on real data that both decode to identical records.
- **Gaps**: in A03 a gap is "two disjoint Series_Period classes" (curvetype document, section 5) and the knowledge base confirms ("If there is missing data within the queried period, there will be 2 timeIntervals included in the XML"); positions between Periods get no record. An A01 Period with a missing position (schema violation) yields no record for it; positions before the first A03 Point likewise. Gaps stay gaps.
- **Not available**: a Point without a value element means "not available" (knowledge-base example: "activation_Price.amount is not defined for this point and therefore the value is considered as not available", and following A03 positions share it). The record keeps value None: an explicit null.
- **Reason and cancellation**: `Reason` codes on a TimeSeries or Point and the GL `cancelledTS` flag are kept on the record for the adapter to act on; the decoder never drops data because of them.
- **Repeated TimeSeries** (seen live for NO2 prices from 2026-08-07, fixture `day_ahead_prices__no2__2026-08-08__duplicate_timeseries`): the document holds two TimeSeries identical except for `mRID` (1 and 2), with the same value at all 96 intervals. The adapter collapses records equal in every field but the TimeSeries mRID; two records for one series and interval that differ in any field raise ParseError naming the zone, interval and both values. Nothing is ever picked silently.
- Everything else unexpected (unknown root or curve type, unknown Point element, duplicate or out-of-range position, a Period that is not a whole number of resolutions, a timestamp without UTC designator, DTD or entity declarations, zip) raises ParseError. Namespace versions are not pinned (7:0 and 7:3 both occur in official examples).

## Base-tier design (ADR 0011)

- `registry/countries/eu.yaml`: pseudo-country, status `reference`, no zones. ENTSO-E codebook entries live in `codebook/eu/entsoe.<Dataset>.yaml` with ids `eu.entsoe.<Dataset>.<field>[.<dim>]`, because `ref.codebook_entry.country_code` must reference a country. Its timezone, Europe/Brussels, defines the local day for base-tier daily partitions (SDAC delivery days are CET; EU clocks change at the same UTC instant).
- Manifests (`src/scanner/adapters/entsoe/manifests/`) set `zones_from_registry` to a scope (`active`, `active_and_reference` or `all` countries) and get every zone with a verified EIC in it, keyed by EIC, when loaded; adding a country stays "add `registry/countries/<cc>.yaml`" (`tests/test_agnostic.py`). Prices use `all`, load and generation `active`. The manifest `zone_field` names the record field holding the zone's EIC: `in_domain` (prices), `out_bidding_zone` (load), `in_bidding_zone` (generation). `resolution: per_period` makes each resolution of a zone its own series (key suffix such as `.pt15m`, ADR 0002).
- Where Energi Data Service covers the same zone and class, the country file's `precedence` decides which source features read (`scanner.features.inputs`); dk, de, no and se list `energidataservice` first, so Danish features and the reference population are unchanged.
- Zone EICs are verified against the area list ([Area List with EIC](https://transparencyplatform.zendesk.com/hc/en-us/articles/15885757676308-Area-List-with-Energy-Identification-Code-EIC)) and the EIC registry (<https://eepublicdownloads.blob.core.windows.net/cio-lio/csv/Y_eicCodes.csv>). `make contracts` now checks every zone EIC's format, check character (algorithm of the [EIC Data Exchange IG v1.2](https://www.entsoe.eu/Documents/EDI/Library/cim_based/EIC_Data_Exchange_IG_v1.2.pdf); it holds for 1827 of the 1829 Y codes in the registry) and uniqueness.

## Backfill

Limits: 400 requests per minute per token (the adapter sends at most 2 per second per process), one year per request, and a ban of about 10 minutes after a 429 (the client honours `Retry-After`). History starts 2025-03-04 in all three manifests.

`scanner.cli ingest` runs one daily run per local day, and each run fetches its own window (day plus lookback and lookahead, 3 days). To avoid one request per zone per day, the adapter reuses responses the way the Energi Data Service adapter does for its retired dataset (`_retired_responses`):

- A run whose window ended at least 7 days ago (`REUSE_MIN_AGE`) fetches a wider window per zone, `Query.reuse_span` from the run's fetch start: 31 days for prices and load, 7 days for generation (about 30 series per day, and every run decodes the whole response it used). Later daily runs inside that window in the same process get the same response and make no request. One response is kept per dataset and zone; only 2xx responses are kept.
- A run whose window ends within the last 7 days, today included, is never widened and never served from a reused response, so scheduled runs are unchanged. Corrections to published data are caught by the lookback.
- Every run records the response it used in the raw store, with the wide `periodStart`/`periodEnd` in its own sidecar. The bytes of a reused response are stored once; the later runs' sidecars point to them (`content_uri`, `scanner.core.raw_store`). `parse` clips to the run's own window, so a run is rebuilt from its capture alone. The fingerprint is computed from the records of the partition day only (the size field is the stored response size and is not compared). The token is never in a capture, as before.
- Processes do not share the reuse; do not run one dataset in two processes over the same days.

Request count for 2025-03-04 to 2026-10-18 (594 days; the last 7 days run one request per zone per day), estimated, not measured:

| Dataset | Zones | Before | Now | Expected duration |
|---|---|---|---|---|
| EnergyPrices | 15 | about 8,900 | about 420 | 15 to 20 min |
| ActualTotalLoad | 3 | about 1,780 | about 85 | 3 to 5 min |
| ActualGenerationPerType | 3 | about 1,780 | about 375 | 15 to 20 min |

Durations assume about 2 s per request including the 0.5 s throttle; the database work per day adds a few seconds per day. Raw store growth: one wide response per zone and reuse window plus a small sidecar per run, since identical bytes are stored once (from 2026-10-18; before that each run stored its own copy, roughly 1 to 2 GB for prices and 0.5 GB for generation).

Commands (the token is read from `ENTSOE_API_TOKEN` in the environment):

```
uv run python -m scanner.cli ingest --dataset eu_entsoe_energyprices --from 2025-03-04
uv run python -m scanner.cli ingest --dataset eu_entsoe_actualtotalload --from 2025-03-04
uv run python -m scanner.cli ingest --dataset eu_entsoe_actualgenerationpertype --from 2025-03-04
```

The three can run together with `--dataset all --workers 3` (the throttle is per process, so the 400 per minute limit stays far away). Check dataset names with `uv run python -m scanner.cli ingest --dataset x` (it lists them).

## Decisions

The normalisation decisions for this source, in one place (the ingest assets show this section in Dagster, `scanner.assets.rules`; details and evidence in "Decoding", "Base-tier design" and the codebook notes):

- **Time.** Request and response times are UTC; a Point at position p covers `[start + (p - 1) * resolution, start + p * resolution)` of its Period. The record field `interval_start` is the manifest's UTC time field.
- **Resolution per period.** Every Period keeps its own resolution and each resolution of a zone is its own series (`resolution: per_period`, ADR 0002): DK1/DK2 prices PT60M up to delivery day 2025-09-30, PT15M from 2025-10-01.
- **A03 expansion.** A curveType A03 block is expanded into one record per position with the block's value, because the source states that value for every position of the block; this decodes the format and is not forward-filling. Positions between Periods and before the first Point get no record. Pending owner sign-off (open question 1); on DK1 2026-10-14 the A03 and A01 responses decode to identical records.
- **Duplicate-series collapse.** Records equal in every field but the TimeSeries `mRID` (NO2 prices from 2026-08-07) collapse to one; two records for one series and interval that differ in any other field fail the run.
- **SDAC sequence.** `classificationSequence` 1, or none, is the SDAC day-ahead price; other sequences (DE-LU sequence 2 is another auction) are not ingested. Settled against Energi Data Service for DK1 and DE-LU, assumed for the other zones.
- **Day-ahead only.** `contract_MarketAgreement.type` A01; intraday prices (A07) are not ingested.
- **Units as published, never converted.** The adapter checks the source's unit codes on every record against the codebook unit (`EUR` and `MWH` for prices, `MAW` for MW; `scanner.adapters.entsoe.adapter.UNITS`). Generation and load are average MW per interval, classes `generation_actual_power.*` and `total_load_actual`, kept apart from the energy classes of Energi Data Service (open questions 2 and 3).
- **Not available stays null.** A Point without a value is an explicit null; gaps (missing Periods or positions) stay gaps.
- **Zones from the registry.** Zones are every zone with a verified EIC in the manifest's scope, keyed by EIC; where Energi Data Service covers the same zone and class, the country file's `precedence` lists it first.

## Licence and terms

[Legal Terms and Conditions](https://transparencyplatform.zendesk.com/hc/en-us/articles/40921911218961-Legal-Terms-and-Conditions): Terms of Use of 2023-03-29 (<https://transparencyplatform.zendesk.com/hc/article_attachments/40921869376401>). Clause 3.1: data may be used "for any purpose whatsoever", in good faith, mentioning "the ENTSO-E Transparency Platform as the source of publication of the data", without implying endorsement, and without prejudice to the primary owner's copyright; where there is such a risk, the user must seek the owner's prior agreement, except for data on ENTSO-E's free re-use list.

The list (2023-10-18, <https://transparencyplatform.zendesk.com/hc/article_attachments/40921869379729>) licenses its items under CC-BY 4.0. Of our datasets, physical flows (12.1.G), offered and forecasted capacity (11.1.A) and the balancing items (17.1.x) are on it; **day-ahead prices (12.1.D), actual total load (6.1.A) and actual generation per type (16.1.B&C) are not**. Serving those in a paid product may need the primary owners' agreement (NEMOs for prices, TSOs for load and generation). Decision for the licence owner before Phase 7. Terms page to watch: the Legal Terms and Conditions article above.

## Measured or estimated

Published values are measured, except where the DDD lets TSOs estimate: net generation when unknown and small-scale units without metering (6.1.A, 16.1.B&C). Day-ahead prices are auction results.

## Sensitivity

`public`.

## Codebook entries

`codebook/eu/`: `entsoe.EnergyPrices.yaml` (named after ENTSO-E's data item "Energy Prices [12.1.D]"; class `day_ahead_price`), `entsoe.ActualTotalLoad.yaml` (class `total_load_actual`), `entsoe.ActualGenerationPerType.yaml` (solar, wind onshore, wind offshore; classes `generation_actual_power.*`), `entsoe.PhysicalFlows.yaml`, `entsoe.OfferedTransferCapacity.yaml`. All `draft`.

## Open questions for humans

1. A03 expansion as decoding, not imputation (section "Decoding").
2. ENTSO-E generation is average MW; it has its own classes `generation_actual_power.*` (the `generation_actual.*` classes are energy per interval, equal only at PT60M). F4 for base-tier countries needs a feature-level decision to accept and convert them.
3. ENTSO-E total load versus `consumption_actual`: separate class `total_load_actual`, so F4 has no ENTSO-E input yet.
4. Settled from the recording: SDAC is `classificationSequence` 1 or absent (see the top of this file). Applies to zones not cross-checked (NL, NO2, SE, IT, HU) by assumption.
5. Entity model for border directions (flows, capacities): ADR 0004 has no `border` kind, so no flow or capacity manifests yet (`src/scanner/adapters/entsoe/README.md`).
6. Licence for prices, load and generation in a paid product.
7. The price manifest covers draft countries too (Italy, Hungary): their prices are public and widen the percentile population, but their country files are not yet signed off.

## Risks

The platform migrated to new technology in 2024 to 2025 (R3): the old guide disappeared, the default curve type became A03, rate limiting moved from IP to token. Expect more such changes; the fingerprint checks catch structural ones. The Postman collection warns that its links may change. Data are corrected after publication ("Updates: yes" for prices and flows in the DDD); the lookback window must cover revisions.
