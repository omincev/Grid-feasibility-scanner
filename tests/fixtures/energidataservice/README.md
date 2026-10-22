# Energi Data Service fixtures

AfrrReservesNordic (aFRR capacity market), recorded 2026-10-21, DK1 and DK2 filter, UTC bounds of local days:
`normal_day` 2026-10-14, `dst_end` 2025-10-26, `dst_start` 2026-03-29, `history_start` 2024-10-02 (first DK1 hour,
demand columns null), `demand_start` 2025-08-19 to 2025-08-21 (demand columns begin 2025-08-21), `lookahead`
2026-10-21 to 2026-10-22 (tomorrow's results), `empty` 2026-10-24 (after the last published day).

Real responses recorded on 2026-10-18 with `scripts/record_fixture.py` (WP1.1). Each
`<Dataset>__<case>.json` has a `.meta.json` sidecar with the exact URL, status, fetch time and SHA-256.
Requests use exact UTC bounds of one Danish local day with `timezone=UTC`, `limit=0`, and a DK1/DK2
filter unless noted. Tests: `tests/test_eds_parse.py`, `tests/test_eds_ingest_db.py`.

| Case | Local day | Why |
|---|---|---|
| `normal_day` | 2026-10-14 (Elspotprices 2025-06-10) | recorded without a zone filter to show which zones each dataset publishes (Elspotprices filtered) |
| `dst_end` | 2025-10-26 (Elspotprices 2024-10-27) | 100 quarter-hours or 25 hours |
| `dst_start` | 2026-03-29 (Elspotprices 2025-03-30) | 92 quarter-hours or 23 hours |
| `resolution_switch` | DayAheadPrices 2025-10-01, requested from 2025-09-30T12:00Z; Elspotprices 2025-09-30 and 2025-10-01, no zone filter | hourly ends and 15-minute starts at 2025-09-30T22:00Z |
| `explicit_nulls` | ImbalancePrice 2025-03-14 | null SatisfiedDemand and DominatingDirection 13:45Z to 14:45Z; no null ImbalancePriceEUR exists at the source |
| `history_start` | ImbalancePrice 2025-03-04 | data starts 11:15Z, 47 of 96 quarter-hours |
| `empty` | before or after each dataset's history (Forecasts_Hour: 2024-04-14, permanently missing) | `records: []` |
| `lookahead` | DayAheadPrices 2026-10-17 and 2026-10-18 (D and D+1, as a run on D after the 13:00 publication sees them) | a run for D writes D+1 |
| `spike` | DayAheadPrices 2026-02-16 to 2026-02-18 (D-1, D, D+1) | a real extreme on D, 911.140015 EUR/MWh in DK2 at 16:45Z: a soft deviation that quarantines D only, not the lookback or lookahead days |

## mFRR datasets (recorded 2026-10-21)

`mFRRCapacityMarket`, `MfrrCapacityMarketExtra` and `MfrrEnergyActivationMarket` fixtures were recorded
without the `filter={"PriceArea":["DK1","DK2"]}` the adapter sends (the 429 limits made a second recording
round costly). This is equivalent: all three datasets publish only DK1 and DK2 (checked over the whole
history of both capacity datasets and the recorded EAM days), so the filtered response holds the same
records; only the echoed `filters` field differs. `tests/test_eds_mfrr.py` asserts the zone set.
