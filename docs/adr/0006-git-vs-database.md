# ADR 0006: Git holds what people decide; storage holds what machines produce

Status: accepted, 2026-10-17.

## Context

The system mixes code, human judgement (definitions, matches, claims, assumptions) and machine output (fetched data, features, scores). Reviews, sign-offs and history work best in git. Large or sensitive material does not belong there, and anything in the main repository is visible to factory agents.

## Decision

The main repository holds code, migrations, docs, ADRs, source dossiers, the codebook, country files, entity aliases, market rules, claims extracted from public documents, public assumption sets, benchmarks, small public fixtures, factory templates and prompts, and the beads task graph. Postgres loads curated files from git through `scripts/load_curated.py`, keyed by path and content hash.

Kept out of the main repository:

- data: canonical series, facts, features, scores, estimates (Postgres); raw captures (object storage); Dagster run history and fingerprint baselines (Postgres `ops` schema)
- secrets (server environment or secret manager)
- curated material above `public` sensitivity: NDA site lists, private PPA references, customer parameters; these live in a restricted repository with named access and no factory access, loaded by the same script with a different source path
- holdout tests: a separate repository checked out only by CI
- large files: geometries, network extracts, large fixtures go to object storage and are pinned by SHA-256 in `registry/blobs.yaml`

## Consequences

Every human decision has a reviewed commit. Agents can read everything they need and nothing they must not. Loading curated data is an explicit step that must run after merges (a Dagster sensor watches the deployed commit).
