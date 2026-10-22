# Source dossier: Energi Data Service, FCR capacity markets (FcrDK1, FcrNdDK2)

Status: draft. Country: dk. Owner: Misi. Last verified: 2026-10-21, live against the API and the dataset metadata endpoints.

## What it is

Energinet's published results of the Danish FCR (Frequency Containment Reserve, "primary reserve") capacity auctions: the MW of reserve bought per hour and the capacity price paid per MW. DK1 buys FCR (symmetric, the old "FCR"); DK2 buys the Nordic products FCR-N, FCR-D up and FCR-D down together with Svenska Kraftnat. These are the revenue streams a battery stacks on top of energy arbitrage, so they feed the BESS return range as a reserve-capacity price series. aFRR and mFRR capacity markets are covered in sibling dossiers, not here.

## Access

- Method: REST API, no authentication. Same endpoint, paging, time-window and rate-limit behaviour as `docs/sources/dk/energidataservice.md` (read it first; it is not repeated here).
- Endpoints: `https://api.energidataservice.dk/dataset/FcrDK1` and `.../FcrNdDK2`; metadata at `https://api.energidataservice.dk/meta/dataset/<name>`.
- Rate limits: the generic section applies. Observed on 2026-10-21 for these two datasets: about three calls succeed, then HTTP 429 with `Retry-After` of roughly 120 seconds that counts down (118, 86, 54, 22 s). Sustained throughput during recording was one call per 1 to 2 minutes, far below the nominal 3 calls per 10 s noted in the generic dossier. Plan backfills for that slower figure.
- How it was found: the catalogue `https://api.energidataservice.dk/meta/dataset`, filtered on "FCR" in name or title.

## Datasets

All timestamps come without a `Z`; `HourUTC` is used, `HourDK` never stored. Resolution is one hour (`PT1H`); `HourUTC` is the start of the hour (the source: "00:00 o'clock is the first hour of a given day interval 00:00 - 00:59"). Local days have 23, 24 or 25 hours.

### FcrNdDK2 (chosen, active) "FCR N and D, Frequency Containment Reserves, DK2"

- Description (quoted): "Energinet procures FCR-N, FCR-D up and FCR-D down capacity reserve in collaboration with Svenska Kraftnat in daily auctions where part of the required volume is procured two days before the day of operation, D-1 early (former D-2) and the remaining part is procured the day before the day of operation, D-1 late (former D-1)."
- Fields: `HourUTC`, `HourDK`, `PriceArea` (DK2, SE1, SE2, SE3, SE4; the filter keeps DK2), `ProductName` (`FCR-N`, `FCR-D upp`, `FCR-D ned`; the source's own Swedish "upp"/"ned"), `AuctionType` (`D-1 early`, `D-1 late`, `Total`), `PurchasedVolumeLocal` (MW, "Purchase of primary reserve in local (price) area MW per hour"), `PurchasedVolumeTotal` (MW, all price areas of the auction), `PriceTotalEUR` (EUR per MW, "Marginal price EUR/MW. Same price for all areas").
- Structure: nine records per hour per price area (three products times three auction types). `Total` is the combined volume and a volume weighted price of the two auctions. Verified on 2026-10-14: nine DK2 records at each hour.
- Price semantics: a capacity (reservation) price for holding one MW for the hour, not an energy price. Source comment: "From 01-02-2024: Price (EUR) show marginal price EUR/MW for each Auction Type D-1 early and D-1 late, Auction Type Total show volume weighted average of the marginal prices from D-1 early and D-1 late." Before 2024-02-01 the single price is the same on all three rows of an hour.
- History: `dataFrom` 2021-11-10T00:00 (local); the first records (the first fixture day 2021-11-10 has nulls in 90 of 216 records) hold explicit nulls and stay null. `PurchasedVolumeLocal` is null on 2023-07-01 while `PurchasedVolumeTotal` and the price are present (checked 2026-10-21).
- Update: tagged "Bidaily (P0.5D)", `lastDataUpdate` 2026-10-21T00:54. Energinet: "Making a request every hour or so is sufficient". The data reaches `dataTo` 2026-10-22T23:00Z when read on 2026-10-21 at midday: tomorrow's D-1 early results are published two days ahead, but `D-1 late` rows existed only up to 2026-10-21 (22 of 46 hours per product), so for the furthest day only two of the three auction types exist.
- DST anomalies in the source (recorded fixtures): on 2026-03-29 the hour 21:00Z has no records. On 2025-10-26 the hours are mis-stamped: 00:00Z is missing, the record stamped 01:00Z has `HourDK` 02:00 (the hour that really starts at 00:00Z) and an extra record is stamped 01:01Z (`HourDK` 02:01, against the documented full-hour rule). The adapter rejects any interval start off the manifest's resolution grid (`ParseError`), so that day is a hard deviation and a bead (owner: ask Energinet), not stored data. 2026-03-29 is stored as published (a gap, nothing filled).
- Predecessor: `FcrReservesDK2` is discontinued (data 2009-09-16 to June 2023, a different structure, no products or auction types). Not ingested: the new dataset reaches back to 2021-11-10, which already covers the FCR-D/FCR-N era and avoids joining two structures.

### FcrDK1 (chosen, active) "FCR, Frequency Containment Reserves, DK1"

- Description: "Amount and prices of FCR regulation in bidding zone (Price area) DK1." Tags Active, Daily (P1D), Hour, Price Area (but there is no price area column; the dataset is DK1 only, the manifest has no `zone_field`).
- Fields: `HourUTC`, `HourDK`, `FCRdomestic_MW` ("FCR delivered from Danish market participants in MW"), `FCRabroad_MW` ("FCR delivered from market participants abroad in MW"), `FCRcross_EUR`/`FCRcross_DKK` ("Crossborder settlement capacity price"), `FCRdk_EUR`/`FCRdk_DKK` ("Danish settlement capacity price"). EUR/MW and DKK/MW.
- One record per hour. A single product (FCR, symmetric), no auction types. The metadata does not explain how the cross-border price differs from the Danish price; in the data they are equal in most hours and differ in some, so both are stored as published.
- History: `dataFrom` 2021-01-19; update frequency P1D. Energinet: calling "every five minutes or so between 9:00 and 10:30 CET is sufficient". On 2026-10-21 at midday it held hours up to 2026-10-22T21:00Z (46 records from `start=2026-10-21`), so next-day results are published the previous morning.
- DST anomaly: on 2025-10-26 the hour 01:00Z has no record (24 of 25 hours); stored as published. On 2026-03-29 it has all 23 hours.
- Known null: none seen in the sampled windows; a full-history null scan was not completed because of the rate limit (see Backfill).
- Predecessor: `FcrReservesDK1` is discontinued (from 1999-07-01, "no longer updated"; points to FcrDK1 and regelleistung.net for data after January 2021). Not ingested, history from 2021-01-19 is enough.

### FFRDK2 (chosen, active) "FFR, Fast Frequency Reserve, DK2"

- Description: "Fast Frequency Reserve (FFR) automatic upward regulation reserve in price area DK2. This dataset publish demand, purchase and price for each hour." A fast, upward-only reserve a battery can sell, so it is a revenue stream for the return range.
- Fields: `HourUTC`, `HourDK`, `FFR_DemandMW` (MW demanded), `FFR_PurchasedMW` (MW purchased; can exceed demand, for example 7.65 against 7.43 MW), `FFR_PriceEUR` / `FFR_PriceDKK` (payment per MW per hour). No price area column; DK2 only, manifest has no `zone_field`.
- Resolution PT1H, `HourUTC` the interval start. `dataFrom` 2021-04-26, update P1D, `dataTo` 2026-10-21T23:00Z when read on 2026-10-21 (next day published ahead). Tags Active.
- Predecessor `FFRpurchasedDK2` is discontinued; `FFRdemandDK2` is a separate demand-only dataset that FFRDK2 supersedes (not ingested).
- Backfill: about 2,000 daily partitions (2021-04-26 to 2026-10-21), one request each; same rate-limit caveat as the FCR datasets (days at 1 call per 1 to 2 minutes). Command: `uv run python -m scanner.cli ingest --dataset FFRDK2 --from 2021-04-26`.
- Codebook: `codebook/dk/energidataservice.FFRDK2.yaml`, classes `ffr_capacity.demand`, `ffr_capacity.procured`, `ffr_capacity.price`. Fixture: `FFRDK2__normal_day` only (no DST or history-start fixture was recorded; rate limit).

### Rejected

- `FcrReservesDK1`, `FcrReservesDK2`: discontinued, superseded by the two above (see each).
- `FFRdemandDK2`, `FFRpurchasedDK2`: FFR predecessor or split datasets, superseded by FFRDK2 above.

## Backfill

- History: FcrNdDK2 from 2021-11-10, FcrDK1 from 2021-01-19.
- The adapter makes one request per daily partition for an active manifest (`fetch` in `scanner.adapters.energidataservice.adapter`; the 31-day reuse applies only to `backfill_only`). Each request spans lookback P3D plus the day plus lookahead P1D.
- Request count for a full backfill to 2026-10-21: FcrNdDK2 1,807 partitions, FcrDK1 2,102 partitions, about 3,900 requests in total.
- Maximum span per request: not limited (`limit=0` returned whole windows; a full-history DK2 request of about 400,000 rows was attempted but not confirmed within the rate limit). Responses are small: about 48 KB per FcrNdDK2 day, a few KB per FcrDK1 day.
- Expected duration: at the nominal 3 calls per 10 s about 25 minutes; at the 1 call per 1 to 2 minutes observed on 2026-10-21 for these datasets, 2.5 to 5 days. The second figure is the planning number. If it is too slow, a generic change (reuse one multi-day response across partitions, as the adapter already does for retired datasets) would cut it to about 70 calls, about 2 hours: this is reported to the lead, not done here.
- Commands: `uv run python -m scanner.cli ingest --dataset FcrNdDK2 --from 2021-11-10` and `uv run python -m scanner.cli ingest --dataset FcrDK1 --from 2021-01-19`.

## Decisions

- Time: `HourUTC` is the interval start; interval end is start plus one hour (half-open). `HourDK` is never stored. Implemented in the manifests (`time_field_utc: HourUTC`, `resolution: PT1H`).
- Zones: FcrNdDK2 filters `PriceArea` to DK2 (`dk.zone.dk2`); SE1 to SE4 rows are never fetched. FcrDK1 has no zone column, one zone `dk.zone.dk1`.
- Variants: every product and auction type is ingested as its own series through codebook dimensions (`ProductName`, `AuctionType`), including `Total`. `Total` is not derivable exactly from the two auctions (it is a volume weighted price) and the source publishes it, so we store it as published and do not recompute it. Codebook ids are `dk.eds.FcrNdDK2.<VolumeLocalMW|VolumeTotalMW|PriceEUR>.<product>_<auction>`.
- Units kept as published: MW (held capacity, resampled as a mean, never summed) and EUR/MW (a price per MW per hour). DKK columns are not ingested (duplicates).
- Nulls: explicit nulls stay null (the 2021-11-10 rows, a null `PurchasedVolumeLocal`). Nothing is filled.
- Missing D-1 late rows for the day ahead are expected, not a gap: the next day's D-1 early exists before its D-1 late auction runs. The manifest's `records_per_interval_per_zone: 9` therefore over-counts for the lookahead day only, which the fingerprint does not cover (lookahead days are outside the partition day).
- Revisions: none documented; a three-day lookback and daily lookahead catch late-published rows.
- Needs owner sign-off: the equivalence classes `fcr_capacity.*` and `ffr_capacity.*` in `codebook/classes.yaml` (appended); the meaning of `FCRcross_EUR` against `FCRdk_EUR` for DK1 (undefined in the metadata); whether the DK1 and DK2 series may be compared (DK1 is the symmetric "FCR", DK2 uses the three Nordic products; the classes are separate per product and per dataset, so they are not combined).

## Licence and terms

Same Energi Data Service terms as the other datasets: see `docs/sources/dk/energidataservice-licence.md`. Licence status in the manifests stays "to confirm".

## Measured or estimated

All published directly by the TSO as auction results. Nothing is estimated here. Revenue from FCR is a later valuation step, and needs availability and activation rules that are not in this data.

## Sensitivity

`public`.

## Codebook entries needed

- `codebook/dk/energidataservice.FcrNdDK2.yaml`: 27 entries (3 measures times 3 products times 3 auction types).
- `codebook/dk/energidataservice.FcrDK1.yaml`: 4 entries (domestic and abroad volume, cross-border and Danish price).
- - `codebook/dk/energidataservice.FFRDK2.yaml`: 3 entries.
- New equivalence classes in `codebook/classes.yaml`: `fcr_capacity.<measure>.<product>.<auction>` (DK2; measure price, procured_local, procured_total; one class per auction type so overlapping series stay disjoint), DK1 `fcr_capacity.price.fcr.dk|cross`, `fcr_capacity.procured.fcr.domestic|abroad`, and `ffr_capacity.*`.

## Risks

- Strict per-dataset rate limiting makes backfill slow (see Backfill).
- Product names are in Swedish ("upp"/"ned"); a rename would break the codebook dimensions and trip the fingerprint.
- The FCR-D design changed in the Nordic market (FCR-D dimensioning, auction changes, the move to D-1 early/late auctions); the dataset notes the 2024-02-01 change in what `PriceTotalEUR` means. Further changes would appear in the dataset comment first.
- Energinet's DataHub work (news item 154) lists some datasets; these two were not listed on 2026-10-18 (not re-checked).
