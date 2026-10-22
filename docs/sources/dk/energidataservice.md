# Source dossier: Energi Data Service (Energinet)

Status: draft. Country: dk. Owner: Misi. Last verified: 2026-10-18, live against the API, the dataset metadata endpoints and the OpenAPI spec.

## What it is

Energinet's open data platform. The backbone of the Danish MVP: prices, imbalance and balancing data, forecasts, production and consumption.

## Access

- Method: REST API, no authentication.
- Data: `https://api.energidataservice.dk/dataset/<DatasetName>?start=...&end=...&timezone=UTC&filter=...&sort=...&limit=0`
- Metadata: `https://api.energidataservice.dk/meta/dataset/<DatasetName>`; catalogue of all datasets: `https://api.energidataservice.dk/meta/dataset` (100 datasets on 2026-10-18).
- OpenAPI spec: `https://api.energidataservice.dk/swagger/public/swagger.json` (behind the Swagger UI at `https://api.energidataservice.dk/index.html`). API guide: `https://www.energidataservice.dk/guides/api-guides`.
- Response: JSON `{"total", "filters", "sort", "limit", "dataset", "records": [...]}`. `total` is the number of records returned, not the size of the dataset, so it cannot reveal truncation.

### Time window (verified)

- `start` is inclusive, `end` exclusive. Formats `yyyy`, `yyyy-MM`, `yyyy-MM-dd`, `yyyy-MM-ddTHH:mm`, or dynamic (`now`, `StartOfDay`, `StartOfMonth`, `StartOfYear` plus or minus an ISO 8601 duration, `+` written `%2B`).
- They filter on the dataset's `filterColumn` from the metadata, which is its UTC time column (`TimeUTC`, `HourUTC`, `Minutes5UTC`).
- By default they are read as Danish local time. The API guide: "The date and time refer to the Danish timezone / local Danish time." The OpenAPI spec documents a `timezone` parameter the guide does not mention: "DK (default) or UTC. For interpretation of fixed time values."
- Checked on DayAheadPrices, DK1, `start=2025-10-26&end=2025-10-27`: without `timezone`, 100 rows from `TimeUTC` 2025-10-25T22:00 to 2025-10-26T22:45 (the 25-hour local day); with `timezone=UTC` (or `utc`), 96 rows from 2025-10-26T00:00 to 23:45. An unknown value (`timezone=bogus`) is silently treated as DK, so a typo would shift the window rather than fail.
- The adapter sends exact UTC bounds with `timezone=UTC` and still filters each record on the UTC field.

### Paging (verified)

- `limit=0` returns every matching row. Spec: "Number of records returned (default 100). 0 returns all values". Without `start` and `limit` the API returns 100 rows.
- No server cap was hit: every calendar month of ImbalancePrice from 2025-03 to 2026-10 came back whole with `limit=0` (for example 5,952 rows for May 2025 = 31 × 96 × 2).
- `offset` ("Read from position") exists; the adapter does not need it.

### Rate limits (observed, not published)

- Dataset-specific limits, introduced gradually since May 2026, starting with discontinued datasets (news item 163, "Differentiated rate limits are being introduced on Energi Data Service", `https://api.energidataservice.dk/News`). The guide gives no numbers and warns that IP addresses may be blocked for unnecessary load. Its rule of thumb: one request per update frequency, with `start=now-(3x update frequency)`.
- Observed on 2026-10-18 through the `totalcalls` and `remainingcalls` response headers: active data endpoints allow 3 calls per about 10 seconds; the metadata endpoint 5; the discontinued **Elspotprices allows 1 call per about 5 minutes** (`Retry-After: 205` after a single call).
- Exceeding a limit returns HTTP 429 with a `Retry-After` header (seconds) and the body `{ "statusCode": 429, "message": "Rate limit is exceeded. Try again in 9 seconds." }`.
- The adapter honours `Retry-After` (capped at 330 s) and retries up to 4 attempts.
- For backfill_only (retired) datasets the adapter requests 31 days at a time and reuses that response for the following daily partitions in the same process. A sequential Elspotprices backfill from the CLI (211 partitions) therefore needs about 7 calls, roughly 35 minutes. Each partition's run keeps its own raw reference and sidecar; the bytes of the shared response are stored once (`scanner.core.raw_store`). Dagster runs each partition in its own process, so a Dagster backfill makes one call per partition: about 18 hours.
- Responses carry `cache-control: public, must-revalidate, max-age=60` and `x-cache-status`.

### Errors (observed)

- 400 with a plain-text body for bad parameters: `Invalid column Nope` (filter), `Invalid key in sort expression: [Nope]`, `Invalid datetime format for [start] - valid format: yyyy, yyyy-MM, yyyy-MM-dd, yyyy-MM-ddTHH:mm.`, `Invalid json in filter`.
- 404 `dataset not found <name>`.
- A filter on `null` (`{"ImbalancePriceEUR":[null]}`) gives 400 `Object reference not set to an instance of an object.`; nulls cannot be selected server side.
- The adapter returns 4xx responses, and a 5xx or 429 that survives its retries, to the runtime, which stores them raw and treats them as hard deviations (ADR 0013). A transport failure with no response fails the run and files one bead.

## Datasets

All timestamps come without a `Z` suffix; the `*UTC` field is used, the `*DK` field never stored.

### Elspotprices

Hourly day-ahead prices, discontinued: "This dataset is no longer updated. For data after 2025-09-30 see Day-Ahead Prices." History from 1999-07-01. Fields `HourUTC`, `HourDK`, `PriceArea`, `SpotPriceDKK`, `SpotPriceEUR`. Since 2025-02-01 the DKK price is computed from EUR and the Nationalbank rate. The SYSTEM price stopped on 2025-02-07. The last hour is 2025-09-30T21:00Z (2025-09-30 23:00 local); nothing exists for 2025-10-01. Rate limited to one call per about 5 minutes.

### DayAheadPrices

15-minute day-ahead prices; the first interval is 2025-09-30T22:00Z (2025-10-01 00:00 local), exactly where Elspotprices ends. Fields: `TimeUTC` ("the 'from' timestamp for the period in which the values are valid"), `TimeDK`, `PriceArea`, `DayAheadPriceEUR`, `DayAheadPriceDKK` (computed from EUR). All six price areas are published: DK1, DK2, DE, NO2, SE3, SE4 (2026-10-14 response without a zone filter). Next-day prices appear about 13:00 to 14:00 CET; metadata suggests polling every five minutes in that hour with `start=StartOfDay%2BP1D`. Because a scheduled run materialises today's partition, the manifest's `lookahead: P1D` writes tomorrow's prices on the day they are published.

### ImbalancePrice

15-minute, updated every 15 minutes (metadata suggests `start=now-PT1H` every 10 minutes). Fields: `TimeUTC`, `TimeDK`, `PriceArea` (DK1, DK2 only), `SatisfiedDemand` (MW, positive up, negative down; held Balancing Demand before 2026-03-04), `ImbalancePriceEUR`/`DKK` (null when suspended, published next working day), `SpotPriceEUR`, `DominatingDirection` (JSON integer: 1 up, 0 none, -1 down), `aFRRUpMW` and `aFRRDownMW` (MWh per quarter despite the name), `aFRRVWAUpEUR`/`DKK`, `aFRRVWADownEUR`/`DKK`, `mFRRMarginalPriceUpEUR`/`DKK`, `mFRRMarginalPriceDownEUR`/`DKK`.

Verified on 2026-10-18:

- Data starts **2025-03-04T11:15Z** (12:15 Danish time). The metadata's `dataFrom` (`2025-03-04T12:15:00`) is local time; MASTER_PLAN section 9 repeats it as UTC, which is one hour off. The local day 2025-03-04 holds 47 of 96 quarter-hours per zone.
- **No null `ImbalancePriceEUR` (and no null mFRR price) exists** from 2025-03-04 to 2026-10-18; every month was scanned. Explicit nulls do occur in `SatisfiedDemand` and `DominatingDirection`, for example 2025-03-14T13:45Z to 14:45Z in both zones (fixture `ImbalancePrice__explicit_nulls`) and `SatisfiedDemand` alone on 2026-04-30T15:30Z to 17:30Z.
- **`aFRRDownMW` is published as zero or negative**, although the metadata validation rule says `>=0`. Every recorded value is at most 0. Stored as published.

Regime changes: from 2025-03-19 the imbalance price follows the dominating direction (up: the higher of the aFRR component and the mFRR price; none: spot; down: the lower of the two); before that it equalled the pricing module's imbalance price. From 2026-03-04 the dominating direction is based on Satisfied Demand. Balancing Demand (mFRR SA) moved to the dataset "mFRR Energy Activation Market".

The metadata gives the mFRR marginal price unit as EUR/DKK while the description says EUR/MWh; treated as EUR/MWh and recorded in the codebook entry.

### Forecasts_Hour

Hourly wind and solar forecasts per price area, updated every 5 minutes, derived from Forecasts_5Min. Still hourly after October 2025; no 15-minute successor exists. Fields: `HourUTC`, `HourDK`, `PriceArea` (DK1, DK2 only in the data, although the validation rule lists six areas), `ForecastType` (exactly `Solar`, `Onshore Wind`, `Offshore Wind` in the data; the metadata writes "Onshore wind"), `ForecastDayAhead` (published 18:00 DK the day before, generated 17:50), `ForecastIntraday` ("The forecast for the coming day at 6am Danish time zone"), `Forecast5Hour`, `Forecast1Hour`, `ForecastCurrent`, `TimestampUTC`, `TimestampDK`. Unit "MWh per hour". Offshore Wind covers parks above 100 MW only, smaller offshore parks are in Onshore Wind; Thor is not included; 12 to 16 April 2024 is missing permanently (fixture `Forecasts_Hour__empty`). Solar forecasts are not updated between 18:00 and 06:00. The validation rule says `>0`, but zeros are published.

Rows for tomorrow appear only with the 18:00 day-ahead forecast (checked at 09:05 local on 2026-10-18: rows end at the end of today).

Storage: each forecast column is its own series with `issued_at` set from the documented publication time (day-ahead, intraday) or from `TimestampUTC` (current). `ForecastCurrent` is overwritten at the source; poll and store as issued. A vintage whose documented issue time is after the fetch time is not stored, because the field cannot hold that vintage yet. With `lookahead: P1D`, current-forecast vintages issued today for tomorrow's hours are kept.

### GenerationProdTypeExchange (production and consumption actuals, WP1.3)

Chosen for WP1.3 (open decision D8). Hourly, `TimeUTC`, `PriceArea` (DK1 and DK2 only, although the validation rule lists six areas), from 2015-01-01 to the last complete hour. Ingested fields and classes:

| Field | Class | Source definition (metadata) |
|---|---|---|
| `OnshoreWindPower` | `generation_actual.wind_onshore` | "Average power production from onshore wind power." "All wind generators that are not specifically registered as offshore wind generators." |
| `OffshoreWindPower` | `generation_actual.wind_offshore` | "Electricity production from offshore wind power." "Includes both large offshore parks like Horns reef, and local wind turbines located offshore." |
| `SolarPower` | `generation_actual.solar` | "Electricity production from solar power." |
| `GrossCon` | `consumption_actual` | "Sum of the consumption incl. transmission loss." |

All four in MWh per hour. Not ingested: `SolarPowerSelfCon` (a modelled estimate, 0 in Initial rows), fuel columns, exchange columns, `CO2PerkWh`, `Version`.

Revisions, from the metadata: "Initial: Based on online power signal to Energinet's SCADA system. Published shortly after end of period." "Preliminary: Based on settlement data, however divided on fuels based on last year data per power plant. 8-10 days delay." "Final: ... Published June 1 the following year." The dataset "combines fast updated data with more correct settlement data. Only the best data is shown per time period." In the fixtures 2025-10-26 is Final, 2026-03-29 Preliminary, 2026-10-14 Initial. Each replacement is stored as a new version of the observation (ADR 0003); the label stays in the raw capture.

The DataHub upgrade (news item 154, "DataHub technical upgrade: Impact on Energi Data Service datasets in 2026") lists this dataset: "The URL/endpoint will remain unchanged. However, the values will be calculated using a different method." Expected October/November 2026. Datasets are frozen from 17 September 2026 and backfilled to that date afterwards. In the data, the last Preliminary hour is 2026-09-08T21:00Z (checked 2026-10-18); every hour since the local day 2026-09-09 is Initial.

Manifest: four runs a day, `lookback: P14D` (Preliminary arrives 8 to 10 days after the hour). Two backfills fall outside any lookback: the yearly Final version (re-run the previous year after 1 June), and the DataHub backfill (re-run from 2026-09-09 once news item 154 marks this dataset done).

Comparability caveats for features:

- Forecasts_Hour splits wind at 100 MW (offshore parks of 100 MW or less count as onshore), this dataset at offshore registration. Compare wind totals (onshore plus offshore) between the two, not each type. In the settlement data, offshore parks under 100 MW produce about 3% of offshore output (DK1, 2026-09-20 00:00 local: 57 of 1,895 MWh).
- `SolarPower` excludes estimated self-consumption. Whether the Forecasts_Hour solar forecast includes it is not documented.
- `GrossCon` is lower than ProductionConsumptionSettlement's `GrossConsumptionMWh`, which also counts commercial power self-consumption.

#### Alternatives considered

- **ProductionConsumptionSettlement**: settlement data from DataHub, hourly, DK1/DK2, from 2005. Splits each technology by size: `OffshoreWindLt100MW_MWh`, `OffshoreWindGe100MW_MWh`, `OnshoreWindLt50kW_MWh`, `OnshoreWindGe50kW_MWh`, three solar size bands plus `SolarPowerSelfConMWh`, and `GrossConsumptionMWh`. Most precise, and the only one whose offshore split matches Forecasts_Hour. Not chosen as the primary source for three reasons:
  - It lags 9 to 15 days ("Data is updated with a 9-15 day delay"); the last hour on 2026-10-18 was 2026-10-08T23:00Z.
  - Every class would be a sum of several component series, which the class contract cannot express without double-counting risk.
  - It "may be updated up to 2 years after the specific date", and its calculation method changed on 2026-10-06 (news item 154).
  
  Candidate for cross-source reconciliation (WP2.4) and for an offshore split matched to the forecasts.
- **ElectricityProdex5MinRealtime**: 5-minute SCADA MW per price area (onshore, offshore, solar, exchanges). Rejected: "Errors will occur, and will generally not be corrected", no consumption column (total load would have to be derived), and the metadata caution reports erroneous values at the 2026-03-29 DST switch.
- **PowerSystemRightNow**: 1-minute snapshot of the current system; not per settlement interval and not a history source.
- **ENTSO-E actual generation per type and actual load**: the Phase 3 base tier. A reconciliation partner; not needed for Phase 1.

#### 15-minute successor

None. The catalogue (`/meta/dataset`, 2026-10-18) has no 15-minute production or consumption dataset. ProductionConsumptionSettlement and GenerationProdTypeExchange are still hourly in data up to October 2026. The only 15-minute successor of an hourly dataset since October 2025 besides DayAheadPrices is `CountertradeIntraday_v2`. News item 182 (2026-10-13) says Build Report supports 15-minute data "depending on the time period and dataset". Re-check after the DataHub upgrade lands for GenerationProdTypeExchange, because the new calculation method may come with a new resolution.

### Reserve markets (own dossiers)

The reserve-capacity and activation datasets have dossiers of their own: FCR, FCR-N/D and FFR in `eds-reserves-fcr.md`, aFRR capacity in `eds-reserves-afrr.md`, mFRR capacity and the mFRR energy activation market in `eds-reserves-mfrr.md` (retired and rejected datasets are listed there). Off-grid timestamps (for example FcrNdDK2 at the end of daylight saving 2025-10-26) are rejected by the adapter as a hard deviation.

## Decisions

The normalisation decisions for this source, in one place (the ingest assets show this section in Dagster, `scanner.assets.rules`; details in the sections above and the codebook notes):

- **Time.** Only the `*UTC` column (`TimeUTC`, `HourUTC`) is stored, read as UTC although it has no `Z`; the `*DK` columns are never stored. Requests send exact UTC bounds with `timezone=UTC`, and every record is filtered on its UTC column again.
- **Resolution.** Native and never resampled on ingest: Elspotprices hourly until 2025-09-30 and DayAheadPrices quarter-hourly from 2025-10-01 are two series of the class `day_ahead_price`; the market rule `day_ahead.resolution` says which applies on a day.
- **Currency.** EUR columns only. The DKK columns are computed by the source from EUR and not ingested.
- **Signs as published.** `aFRRDownMW` is stored zero or negative as published, although the metadata validation rule says `>= 0`; `SatisfiedDemand` positive up, negative down; `DominatingDirection` 1 up, 0 none, -1 down.
- **Units as published, never converted.** `aFRRUpMW` and `aFRRDownMW` are MWh per quarter-hour despite their names; the mFRR marginal prices are EUR/MWh (the metadata says EUR/DKK, the description EUR/MWh).
- **Nulls stay null.** Explicit nulls (`SatisfiedDemand`, `DominatingDirection`, a suspended `ImbalancePriceEUR`) are stored as null, never filled.
- **Meanings that changed in place stay one series**, split by market rules: `SatisfiedDemand` held Balancing Demand before 2026-03-04; the imbalance price follows the dominating direction from 2025-03-19.
- **Forecast vintages.** Each forecast column is its own series with `issued_at` from the documented publication time (day-ahead 18:00 local the day before, intraday 06:00 local) or from `TimestampUTC` (current); a vintage whose issue time is after the fetch time is not stored.
- **Revisions.** GenerationProdTypeExchange's Initial, Preliminary and Final values are successive versions of one observation (ADR 0003); the label stays in the raw capture. Lookback P14D covers Preliminary; Final and the DataHub backfill need a re-run.
- **Polling windows.** `recent_lookback` (manifest field): the frequent ticks of ImbalancePrice (every 15 minutes, PT3H) and Forecasts_Hour (hourly, PT3H plus the one-day lookahead) fetch from that long before the tick, floored to the hour. The 01:30 yesterday run of every dataset keeps the full `lookback` (P4D, P2D), so a revision or suspended price published up to four days later is captured by that daily run. Raw growth for ImbalancePrice falls from about 96 MB/day to about 4 MB/day (a poll shrinks from about 1 MB to about 35 KB), plus one full fetch a day.

## Licence and terms

Not stated in the dataset metadata. Energinet's general terms for Energi Data Service still need to be confirmed before Phase 7: attribution, and use in a paid product.

## Measured or estimated

Measured, except:

- the forecasts, which are the source's own;
- `SolarPowerSelfCon` and `SolarPowerSelfConMWh`, which are modelled;
- Initial values in GenerationProdTypeExchange, which are upscaled SCADA measurements.

## Sensitivity

`public`.

## Codebook entries

See `codebook/dk/`.

## Risks

- Datasets get retired and replaced (Elspotprices), and field meanings change in place (Balancing Demand to Satisfied Demand). The fingerprint and metadata watcher must catch both.
- Calculation methods change under an unchanged endpoint (DataHub upgrade, autumn 2026).
- Rate limits are unpublished and tightening, starting with discontinued datasets.
- `timezone` values other than UTC are silently read as DK.
