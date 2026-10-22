# Source dossier: Energi Data Service, aFRR capacity market

Status: draft. Country: dk. Owner: Misi. Last verified: 2026-10-21, live against the API and the dataset metadata endpoints.

## What it is

Energinet publishes the results of the daily Nordic aFRR capacity market (reserved MW and capacity price, up and down, per bidding zone and hour) on Energi Data Service. These are the revenue streams for a BESS that reserves capacity for aFRR (value stacking, valuation inputs). aFRR activation energy is a different product, already ingested through ImbalancePrice. Access, rate limits, time conventions and errors are shared with `docs/sources/dk/energidataservice.md`; this dossier covers only the aFRR capacity datasets.

## Access

- Method: REST API, no authentication. Same terms and polite-use rules as `energidataservice.md`.
- Endpoint: `https://api.energidataservice.dk/dataset/AfrrReservesNordic?start=...&end=...&timezone=UTC&filter={"PriceArea":["DK1","DK2"]}&sort=TimeUTC asc&limit=0`
- Metadata: `https://api.energidataservice.dk/meta/dataset/AfrrReservesNordic`.
- How it was found: catalogue search (`/meta/dataset`) for "afrr", "reserve", "frr" on 2026-10-21.

## Datasets

### AfrrReservesNordic (chosen, active)

"aFRR, Automatic Frequency Restoration Reserves, Capacity Market" (datasetId 145). Tags Active, Daily (P1D), Hour, Price Area; resolution 1 hour (PT1H); filter column `TimeUTC` ("from" timestamp of the hour, no `Z`); `TimeDK` is the local equivalent (not stored). The dataset holds all Nordic bidding areas (the first rows are SE2, SE3, SE4); we ingest `PriceArea` DK1 and DK2.

Fields (metadata names and units): `UpDemandMW` MW, `UpProcuredMW` MW, `UpPriceEUR` EUR per MW, `UpPriceDKK` DKK per MW, `DownDemandMW`, `DownProcuredMW`, `DownPriceEUR`, `DownPriceDKK`. Quote: "Energinet makes daily procurement of automatic frequency restoration reserves (aFRR) on an hourly capacity market ... details on requested demand, procured volumes, and reserve prices across different directions and price areas per hour." Procured note: "procurement includes cross-border allocations. Procurement above demand indicates capacity exports, while procurement below demand indicates capacity imports. Except DK1, which operates as a national market." Dataset comment: "The demand and procured volume may not match exactly."

Regime changes and history (verified by queries):
- DK2 data starts 2022-12-07T23:00Z (2022-12-08 local). DK1 starts 2024-10-01T22:00Z (2024-10-02 local), matching the dataset note "As of 2024-10-02, a new capacity market for aFRR DK1 has been introduced ... 'Nordic' has been removed from the dataset title" (the old name was Nordic only; DK1 previously had the discontinued `AfrrReservesDK1`).
- Upward and downward demand columns carry values only from 2025-08-21 ("contain values starting on 2025-08-21"); before that they are null (explicit JSON null). Recorded: all 48 DK1/DK2 rows of 2024-10-02 have null demand, and the first non-null demand in the 2025-08-19 to 2025-08-21 fixture is 2025-08-21T20:00Z.
- Hourly UTC, so a local day has 24 hours (23 on 2026-03-29, 25 on 2025-10-26); no gaps found in the recorded days. Records for the next local day are published in the morning (the description suggests polling 08:00 to 09:30 CET); `lastDataUpdate` was 2026-10-21T07:42 UTC with data through 2026-10-22 local. DK1 and DK2 both showed 25 hours on the DST end day and 23 on the DST start day.
- Active; `updateFrequency` P1D, `dataTo` is tomorrow's end of day. 391,573 records in total (all areas), 51,983 for DK1+DK2.
- Zero values: procured 0 with a price still published occurs (SE rows on 2022-12-08).

### Rejected

- `AfrrReservesDK1` (discontinued): "no longer updated"; historic Skagerak-based aFRR before the Nordic market, hourly capacity DK1 only up to the 2020s. Superseded by AfrrReservesNordic.
- `AfrrActivatedAutomatic` (discontinued after 2024-10-02, PICASSO go-live): activated energy, not capacity; activation is already covered by ImbalancePrice.
- `AfrrEnergyActivation`, `AfrrPicassoCorrections`, `AfrrBorderAvailableTransferCapacity`, `AfrrLfcActivationLimits`: activation energy, corrections and cross-border limits, not reserved capacity; out of scope for this product (candidates for later, not inspected in detail).
- FCR and mFRR datasets: covered by sibling agents.

## Backfill

- History: 2024-10-02 local for both zones (manifest `history_start_local`). DK2 alone goes back to 2022-12-08; the manifest has one history start for all zones, so the DK2-only period (2022-12-08 to 2024-10-01, about 22 months) is not backfilled. Adding per-zone history starts is a generic adapter change left for a human decision.
- Limits: active-dataset limit about 3 calls per 10 seconds (observed, see `energidataservice.md`); HTTP 429 with `Retry-After` is honoured by the adapter. Page size: `limit=0` returns all rows; one request for the whole DK1+DK2 history is about 52,000 rows, so the span limit is not a constraint.
- Chunking: the adapter makes one request per daily partition (lookback P2D plus lookahead P1D, so about 4 days, about 190 rows). It reuses a response only for backfill_only datasets, which this dataset is not.
- Request count: one per local day from 2024-10-02 to the run date, about 750 on 2026-10-21 (about 200 from 2025-03-04, the project's general start).
- Expected duration: about 750 requests at 3 per 10 seconds is about 40 minutes; allow up to an hour with 429 waits. Command: `uv run python -m scanner.cli ingest --dataset AfrrReservesNordic --from 2024-10-02`.
- Daily operation: the 08:00 to 10:59 Copenhagen ticks every 15 minutes pick up tomorrow's results, with a 13:30 catch-up; the lookback re-reads two earlier days.

## Decisions

- **Time.** `TimeUTC` is the interval start in UTC (no `Z`, read as UTC); `TimeDK` is never stored. Intervals are hourly, half-open, native; never resampled on ingest. Implemented by the generic adapter and manifest (`resolution: PT1H`).
- **Zones.** Only `PriceArea` DK1 and DK2 are ingested; other Nordic areas are filtered out (zone map in the manifest).
- **Currency.** EUR price columns only; DKK columns are computed by the source and not ingested.
- **Units as published.** Volumes MW (hourly capacity); prices stored as `EUR/MW`. The metadata's "EUR per MW" on a PT1H "marginal price for procured capacity" row means EUR per MW for that one-hour interval (EUR/MW/h); recheck if the source moves to 15-minute intervals. Prices use `resample: mean`.
- **Signs.** Down demand and procured volumes are published as positive magnitudes and stored as published (unlike activated downward energy in ImbalancePrice, which is non-positive).
- **Nulls stay null.** Demand columns before 2025-08-21 are explicit nulls and stay null; zero procured volume is a real zero.
- **Procured vs demand.** Procured includes cross-border allocations and can differ from demand; we do not reconcile or net them.
- **Zone history.** DK2 history from 2022-12-08 is not loaded because the manifest has one history start (2024-10-02) for both zones; a per-zone history start is a later adapter change.
- **Revisions.** The source does not state that results are revised; the two-day lookback re-reads recent days, and changes appear as revisions.
- **New classes.** `afrr_capacity.{demand,procured,price}.{up,down}` equivalence classes are proposed in `codebook/classes.yaml`; owner sign-off pending.

## Licence and terms

Same Energi Data Service terms as the other datasets (`docs/sources/dk/energidataservice-licence.md`); to be confirmed by the owner.

## Measured or estimated

All values are published directly by Energinet (auction results). Nothing is estimated here. Revenue estimates (price times procured capacity times availability) belong to valuation.

## Sensitivity

`public`.

## Codebook entries needed

`codebook/dk/energidataservice.AfrrReservesNordic.yaml`: UpDemandMW, UpProcuredMW, UpPriceEUR, DownDemandMW, DownProcuredMW, DownPriceEUR (DKK columns and TimeDK listed as not ingested).

## Risks

- The dataset was renamed (Nordic dropped) and extended with DK1 in October 2024 and gained demand columns in August 2025; further column additions are likely.
- Capacity price unit is read as EUR/MW/h for the hourly interval; recheck if the interval changes.
- Differentiated rate limits are being rolled out; the 31-day response reuse is not available for active datasets, so a backfill is about 750 calls.
- A market redesign (new Nordic aFRR CM rules) may change the products or resolution.
