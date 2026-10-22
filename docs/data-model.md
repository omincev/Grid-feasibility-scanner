# Data model

The authoritative definition is the SQL in `db/migrations/`. This file explains the intent of each schema and table so that agents write to the right place.

## Schemas

| Schema | Holds | Written by |
|---|---|---|
| `ref` | countries, zones, entities, aliases, sources, datasets, codebook entries, market rules, assumption sets, benchmarks | `scripts/load_curated.py` from git |
| `core` | series, observations, facts, claims, source documents, estimates | ingest and estimate assets |
| `feat` | feature values, scores | feature and score assets |
| `val` | valuation runs and results | valuation assets |
| `ops` | ingest runs, fingerprints, snapshots, quarantine decisions, bead outbox | runtime |
| `serving` | views for front end and API | migrations |

## Time series

`core.series` has one row per distinct series. Its natural key `series_key` is `<cc>.<source>.<dataset>.<zone_or_entity>.<field>` (for example `dk.eds.DayAheadPrices.dk1.DayAheadPriceEUR`). Columns: measure (codebook entry ID), entity ID, unit, resolution (interval), kind (`actual`, `forecast`, `settlement`, `capacity`), sensitivity, licence.

`core.observation` has one row per series, interval, issue time and knowledge time:

| Column | Meaning |
|---|---|
| `series_id` | FK to `core.series` |
| `interval_start`, `interval_end` | valid time, UTC, half-open |
| `issued_at` | when the source issued a forecast; null for actuals |
| `ingested_at` | when we stored this version |
| `value` | double precision; null means the source published an explicit null |
| `run_id` | the ingest run |
| `quality` | `ok` or `quarantined` |

Gaps (intervals the source never published) have no row; `ops.ingest_run` records expected and received counts so gaps are visible. The loader inserts a row only when the value differs from the latest stored version for that key. `core.observation_latest` returns the latest version per key; `core.observation_asof(ts)` returns what was known at `ts`.

Quarantined rows are reviewed per ingest run in `ops.quarantine_decision` (`accept` or `reject`, with `decided_by`, `reason`, `decided_at`; append-only, the run's latest decision is in force), never by updating `quality`. Readers use `core.observation_effective` (the latest version plus `review_status`: `ok`, `unverified`, `verified` or `rejected`, and `usable`, false only for rejected) or `core.observation_effective_asof(ts)`, which applies only decisions made by `ts`. See `docs/quality.md` layer 2 and `scanner.runtime.quarantine`.

## Sparse knowledge

`core.source_document` is every document or page capture we hold: URL, fetch time, SHA-256, storage URI, content type, sensitivity.

`core.fact` stores an attribute of an entity: `entity_id`, `attribute` (codebook entry), one of `value_num`, `value_text`, `value_json`, unit, `valid_from`, `valid_to`, `known_at`, `source_document_id` or `claim_id`, sensitivity. Capacity-map categories, register entries, queue figures and connection regimes are facts. An explicit null from the source is a JSON null in `value_json`. The current value of an attribute is the fact with the latest `valid_from`, then the latest `known_at`; writers append only changed values (`scanner.core.db.write_facts`).

`core.claim` stores a figure taken from a document: the value, the quote, page and location, extraction method (`parser`, `llm`, `manual`), model ID and prompt hash, status (`auto`, `pending_review`, `approved`, `rejected`), reviewer. Claims curated in git are loaded with status `approved`.

`core.estimate` stores an inferred value: target entity or zone, measure, period, `low`, `mid`, `high`, method and version, input references, run ID, sensitivity.

## Reference

`ref.entity` holds every entity with kind, parent, country and optional EIC code; geometry lives in `ref.entity_geometry` (PostGIS). Each country file also yields an entity `<cc>.country.<cc>`, the parent of its zones. `ref.entity_alias` maps source names to our IDs with the approver. `ref.codebook_entry` mirrors `codebook/`. `ref.market_rule` mirrors `market_rules/`. `ref.assumption_set` and `ref.benchmark` mirror their folders. `ref.scenario` mirrors `assumptions/scenarios/` (sets per role, method switches, overrides, status; at most one `official`), `ref.method_card` the method cards in `docs/methods/` (front matter and body). `ref.source` lists every source named by a manifest or codebook file, with its dossier path. `ref.dataset` holds the freshness metadata of every manifest (status, schedule, SLA, resolution, licence, sensitivity) and every entry of `registry/snapshots.yaml` (source `snapshots`), for `serving.source_freshness`.

`ref.curated_file` is the loader's bookkeeping (`scanner.core.curated`): one row per loaded file with its SHA-256 and the natural keys it produced, so unchanged files are skipped and entries removed from files are found. Removed entries are deleted only when no other row references them; the load report lists the ones kept.

## Features, scores, valuation

`feat.feature_value`: feature key, entity, period, value, run ID, code version, sensitivity. `feat.score`: score kind (`congestion`, `grid_friendliness`), entity, period, value, breakdown (JSON with each feature's raw value, percentile, weight and contribution), weights version, run ID. `val.run` and `val.result` hold valuation inputs (asset template, assumption set, entity, price series window, method) and outputs (metric, low, mid, high).

Every `run_id` in `feat.feature_value`, `feat.score`, `feat.opportunity` and `val.run` resolves (foreign key) to `ops.compute_run`: kind (`feature`, `score`, `valuation`), name, start and finish time, status (`ok`, `partial`, `skipped`, `failed`), code version, parameters such as the analysis window, the scenario of score and valuation runs (`scenario_id`), and the reason when outputs were skipped. Open and close runs with `scanner.runtime.provenance.compute_run`. Feature statistic names are listed in `scanner.features.zone`.

Methods and scenarios: `feat.feature_value` rows record `method_id` and `method_version`. Rows of `feat.score`, `feat.opportunity`, `val.run` and `val.result` record `scenario_id`, `scenario_hash` (content hash of the scenario file), `assumption_hashes` (`{set_id: content hash}` of the sets used), the method (`method_id`, `method_version`; `val.run.method`) and `method_versions` (`{method_id: version}` of the row's method and the methods upstream of it). Features are scenario-independent; scores and valuations are computed once per scenario (`scanner.runtime.scenarios`).

## Serving

Views in `serving` are the only objects the anonymous role can read. They filter to `sensitivity = 'public'`, join display names, and include freshness and quality flags so the UI can label stale or unverified values. Their columns are a contract with the front end, documented in `docs/ui-contract.md` and checked against the database by `tests/test_serving.py`. Helper views they share (`feat.latest_score`, `feat.score_component`, `feat.feature_catalog`, `feat.f7_latest`, `feat.opportunity_summary`, `core.preferred_series`, and per scenario `feat.scenario_run`, `feat.scenario_score`, `feat.scenario_score_component`, `feat.scenario_opportunity`, `feat.scenario_return_range`, `feat.official_scenario`) live outside `serving`, so anon cannot read them directly. The card and map views show the official scenario's latest complete run (one run per scenario, kind and name); `serving.scenario_comparison` shows every scenario.
