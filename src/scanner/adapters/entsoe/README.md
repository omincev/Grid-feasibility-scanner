# ENTSO-E adapter (Phase 3, WP3.1)

Dossier: `docs/sources/eu/entsoe.md`. Codebook: `codebook/eu/entsoe.*.yaml`. Manifests: `manifests/*.yaml` (country `eu`).

- `decode.py`: `decode(content) -> list[Record]`, IEC 62325 XML to flat UTC records (one per Period position, A03 blocks expanded, nulls kept); raises `AcknowledgementError` or `ParseError`.
- `client.py`: `get_document(client, params, token)`, one GET with the token in the `SECURITY_TOKEN` header; returns a `FetchResult` safe to store (final 5xx/429 included, for the fingerprint); `redact`, `assert_no_token`, `EntsoeHttpError` (transport failures, redacted, unchained).
- `adapter.py`: `EntsoeAdapter` (fetch, records, parse). Request parameters per dataset in `QUERIES`.

## Manifests

| Dataset | Article | Zones | zone_field | Entries |
|---|---|---|---|---|
| EnergyPrices | 12.1.D (A44) | `zones_from_registry: all` (15 zones today) | `in_domain` | SDAC day-ahead price |
| ActualTotalLoad | 6.1.A (A65/A16) | `active` (DK1, DK2, NL) | `out_bidding_zone` | total load (class `total_load_actual`) |
| ActualGenerationPerType | 16.1.B&C (A75/A16) | `active` | `in_bidding_zone` | solar, wind on/offshore (classes `generation_actual_power.*`) |

`resolution: per_period`: every series takes the resolution of its records and carries it in its key, e.g. `eu.entsoe.EnergyPrices.dk.zone.dk1.price_amount.pt1h` (to 2025-09-30) and `....pt15m` (from 2025-10-01): two series per ADR 0002, concatenated by `scanner.features.inputs.load_input`. Completeness is counted per series present on the day; an empty past day is a hard deviation.

Requests: one per zone per run (a run's window is the day plus lookback and lookahead; longer windows are cut into one-year chunks), sequential, at most 2 per second per process. Daily backfill from 2025-03-04: 15 zones x days for prices, 3 x days each for load and generation.

## Not built: physical flows and transfer capacities

Codebook entries exist (`entsoe.PhysicalFlows`, `entsoe.OfferedTransferCapacity`); no manifests. Missing: an entity for a border direction (ADR 0004 has no `border` kind; `ref.entity.kind` has a CHECK constraint, so it needs a migration and curated border entities with both zones' EICs), and a manifest shape for zone pairs instead of zones. Recorded fixtures for DK1 -> DE-LU flows (A11), offered (A31) and forecasted (A61) capacity are in `tests/fixtures/entsoe/` for when that lands.
