# Source inventory

The backlog of the factory's research and adapter work. Status: `planned` (named, no dossier), `dossier` (dossier drafted), `approved` (dossier approved), `built` (adapter on main), `live` (scheduled in runtime). Access: API, FS (ArcGIS FeatureServer or similar JSON behind a map), PBI (Power BI embed), FILE, PDF, WEB (browser-only), ASK (request from operator).

## Denmark

| Source | What | Access | Measured or estimated | Phase | Status |
|---|---|---|---|---|---|
| Energi Data Service: Elspotprices | day-ahead prices, hourly, to 2025-09-30 | API | measured | 1 | built (backfill only) |
| Energi Data Service: DayAheadPrices | day-ahead prices, 15 min, from 2025-10-01; also DE, NO2, SE3, SE4 | API | measured | 1 | live |
| Energi Data Service: ImbalancePrice | imbalance, aFRR, mFRR, dominating direction, 15 min, from 2025-03-04 | API | measured | 1 | live |
| Energi Data Service: Forecasts_Hour / Forecasts_5Min | wind and solar forecasts (Forecasts_Hour ingested; Forecasts_5Min not) | API | measured (forecasts) | 1 | live |
| Energi Data Service: GenerationProdTypeExchange | onshore and offshore wind, solar, gross consumption per price area, hourly (WP1.3; alternatives in the dossier) | API | measured (Initial values upscaled SCADA, revised to settlement) | 1 | live |
| Energi Data Service: mFRRCapacityMarket, MfrrCapacityMarketExtra | mFRR capacity demand, procured MW, price, up and down, hourly, from 2023-06-20 (Extra from 2025-06-24) (`dk/eds-reserves-mfrr.md`) | API | measured | 2 | built (not yet deployed) |
| Energi Data Service: MfrrEnergyActivationMarket | mFRR EAM activations (SA, DA, total, offered), 15 min, from 2025-03-04 (`dk/eds-reserves-mfrr.md`) | API | measured | 2 | built (not yet deployed) |
| Energi Data Service: AfrrReservesNordic | aFRR capacity demand, procured MW, price, up and down, hourly, DK1 and DK2 from 2024-10-02 (DK2 history from 2022-12 not loaded) (`dk/eds-reserves-afrr.md`) | API | measured | 2 | built (not yet deployed) |
| Energi Data Service: FcrNdDK2, FcrDK1, FFRDK2 | FCR-N and FCR-D up and down (DK2, per auction), FCR (DK1), FFR (DK2): procured MW and price, hourly, from 2021 (`dk/eds-reserves-fcr.md`) | API | measured | 2 | built (not yet deployed) |
| National capacity map (BEK 1468/2025 §29; Energinet and Green Power Denmark, `Kapacitetskortet_V2`) | free MW and limited-access ranges per DSO and TSO station, both directions; no category or flexible-connection field is published | FS (one shared service for all DSOs) | measured (MW, indicative) | 1 | live (snapshots and parsed facts, weekly) |
| Energinet connection-queue map | queue MW by region, direction and phase, monthly | PBI (public query) | measured (regional) | 1 | live (snapshot and parsed facts, monthly) |
| Energinet transmission capacity information | TSO station capacity, free bays, line traces (layers of the national capacity map) | FS | measured | 1 | live (TSO consumption figures treated as not assessed; see `docs/questions/`) |
| ENTSO-E Transparency | day-ahead prices (15 zones), load and generation per type (DK1, DK2, NL) live; flows, transfer capacities, balancing (dossier only) | API (token) | measured | 3 | live |
| Danish Energy Agency master data | turbines and plants with location, size, start date | FILE | measured | 3 | planned |
| DMI | weather observations and forecasts | API | measured | 3 | planned |
| Datafordeler (DAGI) | municipality boundaries (curated geometry, `scripts/build_geometries.py`) | API (key) | measured | 1 | built |
| GeoDanmark / Dataforsyningen | base map, cadastral references | API | measured | 3 | planned |
| DSOs (Radius, N1 and others) | transformer data, reinforcements | ASK | measured | 3 | planned |
| Nord Pool | intraday prices | API (paid) | measured | open D9 | planned |

## Italy

| Source | What | Access | Phase | Status |
|---|---|---|---|---|
| ENTSO-E | base tier for all Italian bidding zones | API | 3 | dossier |
| Terna | load, generation, balancing, curtailment, connection requests (Econnextion) | to research | 5 | planned |
| GME | day-ahead and intraday prices, MACSE results | to research | 5 | planned |
| ARERA | reports, curtailment (e.g. Relazione 301/2026/I/EFR), DSO flexibility pilots | PDF | 5 | planned |
| DSOs (e-distribuzione and others) | hosting capacity | to research | 5 | planned |

## Hungary

| Source | What | Access | Phase | Status |
|---|---|---|---|---|
| ENTSO-E | base tier, including balancing data that replaced discontinued MAVIR publications | API | 3 | dossier |
| MAVIR | system data, balancing, capacity allocation | to research | 5 | planned |
| HUPX | day-ahead and intraday prices | to research | 5 | planned |
| MEKH | market monitoring (Power BI) | PBI | 5 | planned |
| DSOs (E.ON, MVM, OPUS, ELMŰ) | capacity publications | to research | 5 | planned |

## Cross-country

| Source | What | Access | Phase | Status |
|---|---|---|---|---|
| PyPSA-Eur | open transmission network model | FILE | 5 | planned |
| OpenStreetMap power infrastructure | lines and substations | API | 5 | planned |
| EU ETS installation list, IPPC permits | industrial sites for DSF | FILE | 6 | planned |

## Backlog (after foundations are robust)

Named after the expert review of 18 October 2026; not researched yet. No dossier, no access method checked, no phase assigned. Each needs a dossier (and, for the last one, a sensitivity decision) before any adapter work.

| Source | What | Access | Phase | Status |
|---|---|---|---|---|
| Future grid capacity | planned connections and approved reinforcements per station (for example the capacity map's `reinforced_grid` rows and network development plans), so F7 can show capacity after reinforcement | to research | not set | planned |
| Connection queues | Energinet's queue map parsed into MW per region and phase, beyond the snapshots captured today (`energinet-connection-queue.md` covers the snapshots) | to research | not set | planned |
| Grid tariffs and taxes | connection charges, network tariffs (DSO and Energinet), electricity taxes and their exemptions per asset type | to research | not set | planned |
| Intraday prices | Nord Pool intraday (paid licence, open decision D9) | API (paid) | not set | planned |
| Reserve capacity markets outside Denmark | FCR, aFRR and mFRR capacity for other countries (Denmark: see the Denmark table) | to research | not set | planned |
| DSO contract data | connection agreements and contract terms shared by DSOs; private, sensitivity `nda` or `customer`, kept in the restricted repository only, never in this one or in agent context | ASK | not set | planned |
