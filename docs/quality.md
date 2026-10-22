# Quality and testing

Five layers of checks. Each section states whether it is implemented or planned. Run results are visible in Dagster; hard deviations and parser failures write to `ops.bead_outbox`.

## 1. Adapter tests (factory side, CI)

Every adapter has offline tests against recorded fixtures in `tests/fixtures/<source>/`. Required cases: a normal day, both DST transitions, any resolution switch, explicit nulls, an empty response, and the most recent failure the adapter was fixed for. Tests assert interval counts against `scanner.core.time.expected_intervals`, uniqueness of `(series, interval_start, issued_at)`, UTC conversion, units, and that no value was filled or coerced.

Status: implemented. `make contracts` (`python -m scanner.core.contracts`) validates every manifest, codebook entry, country file, market rule, geometry file, method card, scenario and feature declaration. The database test `tests/test_db.py` checks that every table in `ref`, `core`, `feat` and `val` has a sensitivity column and row-level security.

Planned: holdout tests in a separate repository that CI checks out and workers never see, holding values a person looked up by hand on the source website and the acceptance tests for each bead. The CI job exists but is disabled until that repository exists.

## 2. Run contracts (runtime, every run)

Fingerprints per ADR 0013. Hard deviations block the write. Soft deviations quarantine: the rows land with `quality = 'quarantined'` and every reader still uses them, labelled unverified, because they are usually real market events. A person reviews the ingest run with `python -m scanner.cli quarantine` (list with corroboration evidence per layer 3, decide, accept-corroborated): accept clears the label (verified), reject excludes the rows from every reader (a genuine data error, such as a DKK value in a EUR field). Readers derive this from `core.observation_effective`; features, scores and valuations record how many unverified values they used. Baselines warm up over 14 runs. Thresholds per manifest, defaults in `scanner.core.fingerprint.DEFAULT_THRESHOLDS`:

- row count outside the expected interval count: hard if zero, soft otherwise
- schema: any added, removed or retyped field is hard
- null rate per field above baseline P95 plus 0.05: soft
- numeric value outside baseline [P0.1 minus 3 IQR, P99.9 plus 3 IQR]: soft, never hard
- arrival later than the freshness SLA: shown as `stale` in `serving.source_freshness`; interior gaps per dataset are in `serving.source_completeness`, and `scanner.cli catch-up` re-ingests missing days. Alerting a named owner is planned (WP2.8).

Status: implemented, except alerting.

Scope of a quarantine: the fingerprint covers only the partition day, so a soft deviation quarantines only rows whose `interval_start` lies in `[day_start, day_end)`; lookback and lookahead rows are written `ok`. A row whose latest version is `ok`, typically written by the previous day's lookahead, gets a new version with the same value and quality `quarantined` under the run that flagged its day, so the review targets that run. The reverse never happens: a later clean run that re-fetches the day (lookback) writes nothing for an unchanged value, so only a decision clears the label. A revised value is always written, with the quality the current run gives its interval (`scanner.runtime.ingest.quarantine_partition_day`, `scanner.core.db.write_observations`).

## 3. Cross-source reconciliation

Where two series share a zone and an equivalence class, a daily asset check compares overlapping intervals. Defaults: day-ahead prices must match within 0.01 EUR/MWh after currency alignment; volumes within 1% or 1 MW; forecasts are not reconciled. Three consecutive failures file a bead against the equivalence claim in the codebook.

Status: planned (WP2.4). Today `scanner.cli quarantine list` shows corroboration against an independent series for quarantined runs, and the 18 October verification (`docs/verification-2026-10-18.md`) reconciled EDS and ENTSO-E prices once.

## 4. Outcome benchmarks

`benchmarks/` lists known cases with expected ranges set by domain owners: Ringkøbing-Skjern and Lolland for Denmark, MACSE auction results for Italy, published BESS revenue indices, Energinet pool rankings when published. Every change to feature, score, estimate or valuation code runs the benchmarks as asset checks; a result outside its expected range fails the PR.

Status: planned (WP2.5). Benchmarks load into `ref.benchmark` and are flagged in `serving.ranked_sites` (`is_benchmark`), but expected values are still null and no check runs yet.

## 5. Forecast gate

A forecast model is served only if, on a rolling 90-day backtest using as-of covariates, it beats both "tomorrow looks like today" and the seasonal-naive weekly baseline on pinball loss averaged over the 21 Timestral quantiles, or MAE for point baselines. The gate reruns weekly; a model that falls below the baselines is unpublished automatically.

Status: planned (WP6.7).

## Factory evals

A fixed set of past beads with known-good outcomes is rerun on every change to prompts, models or the supervisor. Track pass rate, rework rate, CI rounds and cost per merged PR. Changes that lower the pass rate do not ship.

Status: planned (WP4.8).
