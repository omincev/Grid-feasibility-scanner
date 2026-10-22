# Source dossier: Energi Data Service, mFRR reserves and energy activation

Status: draft. Country: dk. Owner: Misi. Last verified: 2026-10-21, live against the API and the dataset metadata endpoints.

## What it is

Energinet's three current datasets for manual Frequency Restoration Reserves (mFRR) on Energi Data Service: the daily capacity auction, the occasional extra capacity auction, and the mFRR Energy Activation Market (EAM). They tell the opportunity scanner what flexible assets earn from reserve capacity (EUR per MW per hour, up and down, DK1 and DK2) and how much balancing energy the system activates (MW per quarter-hour). Uses: BESS revenue stacking (capacity plus activation) and balancing-market tightness features. Access, time window, paging and rate-limit behaviour are shared with the other Energi Data Service datasets: see `docs/sources/dk/energidataservice.md`; this dossier covers what is specific to these three.

The mFRR marginal prices are already ingested through ImbalancePrice (`dk.eds.ImbalancePrice.mFRRMarginalPriceUpEUR`, `...DownEUR`). FCR and aFRR capacity are covered by other dossiers.

## Access

- Method: REST API, no authentication, no key.
- Endpoint: `https://api.energidataservice.dk/dataset/<DatasetName>?start=...&end=...&timezone=UTC&sort=TimeUTC+asc&limit=0`; metadata at `https://api.energidataservice.dk/meta/dataset/<DatasetName>`.
- Authentication: none.
- Rate limits: as in the main dossier (observed 3 calls per about 10 seconds on active datasets). `MfrrCapacityMarketExtra` was stricter during the research: a burst of five calls was answered with 429 `Rate limit is exceeded. Try again in 112 seconds.` and later calls needed pauses of minutes. The adapter honours `Retry-After`.
- How it was found: the catalogue `https://api.energidataservice.dk/meta/dataset` filtered for mFRR in name or title (2026-10-21).

## Datasets

Common to all three: JSON `{"total","filters","sort","limit","dataset","records"}`; `TimeUTC` (the interval start, UTC, no `Z`) and `TimeDK` (not stored); `PriceArea` holds only `DK1` and `DK2` (checked over the whole history of the two capacity datasets and a full day of the EAM, although the metadata's validation rule lists more codes). One record per zone and interval.

### mFRRCapacityMarket (chosen)

Metadata `datasetId` 146, title "mFRR, manual Frequency Restoration Reserves, Capacity Market", tags Active, Bidaily (P0.5D), Hour, Price Area. Resolution **hourly (PT1H)**. Metadata `dataFrom` 2023-06-21T00:00:00 (local); first `TimeUTC` **2023-06-20T22:00Z**. `dataTo` is the end of tomorrow: the daily auction result for the next delivery day is in the dataset by the afternoon (`lastDataUpdate` 2026-10-21T13:28 when `dataTo` was 2026-10-22T23:00). The source suggests polling with `start=StartOfDay-P2D` about hourly.

Fields (source definitions in the codebook): `UpDemandMW`, `DownDemandMW` (local demand sent to the market, MW), `UpProcuredMW`, `DownProcuredMW` (procured MW; includes cross-border allocation: above demand means export, below means import), `UpPriceEUR`, `DownPriceEUR` (marginal price, "EUR per MW" in the metadata), and DKK duplicates `UpPriceDKK`, `DownPriceDKK` (not ingested).

Verified on 2026-10-21 over the whole history (58,512 records, 29,256 per zone):

- Regime: from 2024-11-19 Denmark, Sweden and Finland run a trilateral Nordic capacity market; since January 2025 demand is set daily by dynamic dimensioning. In DK2 an additional monthly auction exists that is **not** in this dataset.
- Demand columns are null before 2024-02-28 (12,050 nulls per column, none elsewhere) by the source's own note. Procured and price columns are never null.
- **Gap at the source:** no record from 2023-06-22T21:00Z up to 2023-06-23T22:00Z, i.e. the whole local day 2023-06-23 is missing in both zones; otherwise contiguous hourly, no duplicates.
- Range: up price 0.27 to 4,100.59 EUR/MW, down price 0 to 2,631.25; up procured 71 to 661 MW, down procured 0 to 178.
- DST days carry 23 or 25 hourly records as expected (fixtures).

Predecessors `MfrrReservesDK1` and `MfrrReservesDK2` are retired ("no longer updated"; data before June 2023). Not ingested: history before 2023-06 is outside the product's need, and the structure (per area, different reserve design) is not comparable.

### MfrrCapacityMarketExtra (chosen)

Metadata `datasetId` 150, "mFRR, manual Frequency Restoration Reserves, Capacity Market, Extra Auctions". Hourly (PT1H). First record **2025-06-24T00:00Z** (`dataFrom` 2025-06-24T02:00 local). Same nine fields as above. Held only when the forecast-based daily procurement proves too low, in an afternoon auction; the dataset nevertheless carries a record for every hour: zero demand and procured, null prices when no auction took place.

Verified on 2026-10-21 (23,328 records, 11,664 per zone, contiguous, no duplicates): demand columns are null before 2025-08-21 (2,828 per column); `UpPriceEUR` null in 20,136 and `DownPriceEUR` in 22,796 records (no procurement); up price up to 1,974.20 EUR/MW; procured up to 105 MW up and 60 MW down. Procurement occurred on 342 distinct days, recently on 2026-10-19, 20 and 21, so the auction is frequent enough to matter for revenue.

This dataset and `mFRRCapacityMarket` are different auctions: values must not be summed or compared without a feature that declares both. They have separate equivalence classes (`mfrr_capacity.*`, `mfrr_capacity_extra.*`).

### MfrrEnergyActivationMarket (chosen)

Metadata `datasetId` 158, "mFRR Energy Activation Market", tags 15 Minute, Active, Every 15 minutes (PT15M). Resolution **15 minutes (PT15M)** throughout. First record **2025-03-03T23:00Z** (local midnight 2025-03-04, the Nordic EAM go-live); no date split in the dataset. The source states the dataset is dynamic: values of an interval can change before and during the market time unit (scheduled activations, direct activations). It indicates the expected dominating direction; the final dominating direction is in ImbalancePrice (from 2026-03-04 based on satisfied demand).

Fields, all integer MW unless noted: scheduled activation `mFRRSAUpReqMW`, `mFRRSADownReqMW` (zone's request to the Nordic Activation Optimisation Function), `mFRRSAUpEUR`, `mFRRSADownEUR` (EUR per MWh, marginal AOF price); direct activation `mFRRDAUpMW`, `mFRRDADownMW`, `mFRRDAUpEUR`, `mFRRDADownEUR` (separate local clearing, "typically used for direct activation or fall back"); `TotalmFRRUpMW`, `TotalmFRRDownMW` (scheduled plus direct activation); `mFRROfferedUpMW`, `mFRROfferedDownMW` (sum of offered volumes); `mFRRLocalUpMW`, `mFRRLocalDownMW`; `mFRRSpecialUpMW`, `mFRRSpecialDownMW` (special for local congestion).

Verified on 2026-10-21 against fixtures for 2025-03-04, 2025-10-26 (100 intervals per zone), 2026-03-29 (92) and 2026-10-14:

- **Scheduled-activation prices duplicate ImbalancePrice.** `mFRRSAUpEUR` and `mFRRSADownEUR` equal `ImbalancePrice.mFRRMarginalPriceUpEUR` and `...DownEUR` in all 192 zone-quarter-hours of 2026-10-14. They are therefore not ingested (nothing new).
- Never null: the two SA prices, `TotalmFRRUp/DownMW`, `mFRROfferedUp/DownMW`. Null when nothing happened: the SA request volumes (about 60 to 80 percent of intervals), the local volumes, special down. **Direct-activation volumes and prices and special up are null in every recorded interval** (all four recorded days); kept in the codebook so a first value is captured.
- Down prices go negative (down to -70 EUR/MWh on 2025-10-26); that is a published value.
- Volumes are integer MW per quarter-hour; the unit is MW, not MWh. Energy in the interval would be MW x 0.25 h; no conversion is applied.
- `TotalmFRRUpMW` up to 519 MW (SA request volumes up to 535 MW) in the recorded days.

### Rejected or retired

- `MfrrReservesDK1`, `MfrrReservesDK2`: retired, pre-June-2023 history of the capacity market, discontinued.
- `RegulatingBalancePowerdata`: discontinued, replaced by ImbalancePrice and the EAM.
- `RegulatingBalancePowerdata`-style up/down regulation prices: covered by ImbalancePrice, already ingested.
- The monthly DK2 capacity auction: not on Energi Data Service in any of the three datasets (the metadata says so); nothing to ingest.

## Backfill

- History: capacity from 2023-06-21 (local) to today and tomorrow; extra from 2025-06-24; EAM from 2025-03-04.
- Maximum span and paging: `limit=0` returns every matching record. Whole-history capacity (58,512 records, about 16 MB) came back in one call on 2026-10-21; the EAM carries 192 records a day (about 83 KB a day).
- Rate limits: as in the main dossier (3 calls per about 10 seconds), with `Retry-After` honoured. `MfrrCapacityMarketExtra` answered 429 with 112 to 200 second waits after a burst.
- The runtime ingests one local day per partition and fetches `lookback` days before it plus the lookahead; the adapter reuses a response across partitions only for backfill_only datasets. The three datasets are `active`, so a CLI backfill makes one request per partition day.
- Request count: capacity 2023-06-21 to 2026-10-21, about 1,220 days, so about 1,220 requests; extra 2025-06-24 to 2026-10-21, about 485; EAM 2025-03-04 to 2026-10-21, about 597. Total about 2,300.
- Expected duration: with 3 calls per 10 seconds and a few 429 pauses, roughly 4 hours sequentially (capacity 1.8 h, extra 0.7 h, EAM 0.9 h), longer for the extra dataset if its limit is stricter than observed.
- Command: `uv run python -m scanner.cli ingest --dataset mFRRCapacityMarket --from 2023-06-21`; `... --dataset MfrrCapacityMarketExtra --from 2025-06-24`; `... --dataset MfrrEnergyActivationMarket --from 2025-03-04`.
- Known partition outcomes: capacity local day 2023-06-23 is empty at the source (hard deviation by design); the first extra partition (2025-06-24) holds 22 of 24 hours per zone.

## Decisions

- **Time.** `TimeUTC` only, read as UTC although it has no `Z`; `TimeDK` never stored. Requests use exact UTC bounds with `timezone=UTC` (adapter, shared).
- **Resolution.** Native: hourly for both capacity datasets, 15 minutes for the EAM, never resampled. Implemented in each manifest's `resolution`.
- **Currency.** EUR columns only; DKK columns are not ingested (codebook `not_ingested`).
- **Units as published.** Capacity volumes are MW (a power reservation held for the hour), capacity prices EUR per MW (per hour, implicit in the one-hour interval), activation volumes MW per quarter-hour, activation prices EUR/MWh. No conversion.
- **Auctions are separate series.** Daily and extra auctions get separate equivalence classes. Procured volume may differ from demand because of cross-border allocation; it is stored as published.
- **Nulls stay null, zeros stay zero.** Null prices (no procurement) and null demand (before the demand columns existed) are stored as null; zero procured MW is a published value.
- **Duplicates not ingested.** `mFRRSAUpEUR` and `mFRRSADownEUR` equal ImbalancePrice mFRR marginal prices; not stored twice.
- **Revisions.** The EAM is dynamic within the interval: the 15-minute ticks fetch the last three hours and the daily 01:30 run the two-day lookback, so later values arrive as revisions (ADR 0003). The two capacity datasets poll hourly at minute 20 and 25, with a one-day lookahead for tomorrow's auction results.
- **Sign.** All volumes are published non-negative (codebook `sign_convention: non-negative`); down is a separate field (`TotalmFRRDownMW` and others are positive magnitudes). Not flipped.
- **Needs owner sign-off:** the statement that DK2's monthly auction is out of scope; revenue features that stack capacity and activation (not defined here).

## Licence and terms

Energi Data Service portal-wide terms, CC BY 4.0 (the manifests keep the shared "to confirm" wording until the owner confirms), attribution "Source: Energinet (www.energidataservice.dk)": `docs/sources/dk/energidataservice-licence.md`. No dataset-specific condition appears in the three metadata records.

## Measured or estimated

Published directly (auction results, activation volumes and prices). Nothing is estimated from this source. Procured capacity is the auction outcome, not delivered reserve.

## Sensitivity

`public`.

## Codebook entries needed

`codebook/dk/energidataservice.mFRRCapacityMarket.yaml`, `energidataservice.MfrrCapacityMarketExtra.yaml`, `energidataservice.MfrrEnergyActivationMarket.yaml`; equivalence classes `mfrr_capacity.*`, `mfrr_capacity_extra.*`, `mfrr_eam.*` appended to `codebook/classes.yaml`.

## Risks

- Dataset semantics change in place (see the trilateral market on 2024-11-19 and dynamic dimensioning from January 2025); the metadata watcher must catch a changed description.
- The EAM is dynamic and its fields may be added or re-defined; the direct-activation columns are empty today and may fill later.
- A single missing day in the capacity history (2023-06-23) and stricter dataset-specific rate limits (observed on the extra auction).
- Series names (`mFRRCapacityMarket` versus `MfrrCapacityMarketExtra`) differ in capitalisation; the dataset name must be used exactly as published.
