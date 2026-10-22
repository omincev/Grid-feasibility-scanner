# Follow-ups

Living list of known work, decisions and questions after the 18 October 2026 build. Remove an item when it is done; link the PR or bead.

## Before the 21 October demo

- **Data attribution in the UI.** Energi Data Service is CC BY 4.0 and requires "Source: Energinet (www.energidataservice.dk)"; ENTSO-E, Datafordeler/DAGI and the capacity map (Energinet/Green Power Denmark) need their credits too. `docs/ui-contract.md` has no attribution section yet. Add one, ideally with a small `serving.data_sources` view (source, licence, attribution text, URL) so the UI can render it.
- **How Lovable reaches the data:** public HTTPS on the VPS (Caddy, `infra/README.md`) or the static `exports/`.
- **Present proposed outputs as illustrative.** Opportunity matches are empty at the proposed `high_percentile` 75 and BESS ranges carry the "proposed assumptions (not approved)" label until gate H1.
- **Show the capacity-map caveat** (`ranked_sites.capacity_map_caveat[_en]`) on site cards.

## Decisions for people

- **H1 (Oleg, Michael):** `bess_mvp`, `score_weights_v0`, `score_method_v0`, `opportunity_rules_v0`, `saturation_thresholds_v0` values; MW-based colour thresholds for substations (the source publishes MW, not categories).
- **H7 / D1 (Oleg, Michael):** which scenario becomes official (`demo_default`, `f7_direction_aware`, `f7_mw_weighted`, `spec_v0`); BESS as a load-type asset for grid direction; `municipality_min_stations` 3; MW-mean definition of the F7 MW variant; whether a "year" in BESS v0 is 365 consecutive or 365 valued days; F2 clamp per technology or on the total.
- **D1:** widen the price percentile population by adding reference countries (`registry/countries/<cc>.yaml`, config only).
- **Sign-offs:** ENTSO-E A03 expansion treated as decoding; SDAC sequence rule (classificationSequence 1 or none); TSO consumption treated as "not assessed".
- **Licences:** ENTSO-E prices, load and generation are not on its free re-use list (matters for a paid product); capacity-map redistribution is unstated.
- **README** still says "Vorkon's tool" in prose; keep or remove.
- **Alerts:** a named person for stale-source and failed-run alerts (WP2.8).

## Questions to sources

- Energinet: six capacity-map questions in `docs/questions/capacity-map-semantics.md` (TSO consumption zeros, inverted limited-capacity ranges with upper bound 0, flexible-connection indicator, licence, ...).
- Masnedø station (`dk.substation.dso.unlocated.mav_masnedoe`) has coordinates about 1° off and falls outside every municipality; needs a source correction or a curated geometry override.

## Engineering

- **Features use the run country's timezone for every zone** (`scanner.features.run`, `tz = timezones[country]`), and the run country's regime breaks. Fine while all zones are CET; wrong for countries in other timezones. Use each zone's country timezone and rules (ADR 0001, 0011).
- **Country defaults to `dk`** in `features/run.py`, `valuation/run.py` and the CLI; valuation covers only the run country plus reference zones, so NL has no BESS range. Run per active country.
- **Curated data in code:** `INTERZONE_PAIRS` (`features/registry.py`) and the queue-map region to zone mapping (`adapters/energinet/connection_queue.py`) belong in registry or assumption YAML. Stale `TODO(WP1.3)` in `features/registry.py`.
- **Short days in the completeness view:** 132 ENTSO-E generation days and 11 load days short, including 2025-10-26 (DST end) in both. Determine whether these are partial source periods or a counting issue for per-period-resolution series.
- **One zone fails a multi-zone ENTSO-E run** (strict by design). Consider per-zone isolation with partial-day handling (Phase 2).
- **Analytics DB role reads every sensitivity level.** Narrow before any `nda`/`customer` data (ADR 0008).
- **Features recompute full history daily.** Pruning exists (`scanner.cli prune`); skipping unchanged inputs needs input data versions.
- **ForecastIntraday** is issued 06:00 local, after its earliest hours; matters for the WP6.3 foresight penalty.
- `scripts/record_fixture.py` sends no User-Agent.
- `MASTER_PLAN.md` §9 says ImbalancePrice starts 12:15 UTC; it starts 11:15 UTC.

## Phase 2 hardening still open

CODEOWNERS handles and branch protection (WP2.7); anon-key penetration test (WP2.6); outbox drain into `bd` (WP2.9); daily cross-source reconciliation check with beads (WP2.4); benchmarks as asset checks (WP2.5); codebook class enforcement in `make contracts` (WP2.3); terms-of-use watcher beyond Energi Data Service (WP2.2).

## Backlog sources (after foundations)

See `docs/sources/INVENTORY.md`, "Backlog": future capacity, connection queues, grid tariffs and taxes, intraday (Nord Pool), reserve capacity markets, DSO contract data.

## Housekeeping

- `.env.example` is deleted in the laptop working tree (not committed); restore with `git checkout -- .env.example`.
- About 25 merged agent branches and worktrees under `.claude/worktrees/` can be pruned.
- Orchestration helpers (`AGENT_BRIEF.md`, `merge_gate.sh`) live outside the repo in `BESSproject/orchestration/`.
