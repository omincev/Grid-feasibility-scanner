# Adapters

One package per source. Each contains `adapter.py` (implements `scanner.adapters.base.Adapter`), `manifests/<Dataset>.yaml` (schema `scanner.core.manifest.Manifest`), and, for browser sources, `scripts/*.py` Playwright scripts.

An adapter fetches and parses. It never writes to the database, never imputes, never converts units, and raises `ParseError` on anything the manifest and codebook do not describe. The ingest runtime (`scanner.runtime.ingest`) stores raw captures, fingerprints, and writes rows.

Checklist for a new dataset: approved dossier, codebook file, manifest, real fixtures covering the cases in `docs/quality.md`, offline tests, a backfill path that loads the full history through `scanner.cli ingest` within the source's rate limits and query-span limits (documented in the dossier's "Backfill" section), `make contracts && make test` green.

Backfills run through the CLI, not Dagster partition backfills: the CLI can chunk or reuse responses across days (31-day requests for Elspotprices, which allows one call per five minutes), while a Dagster backfill makes one request per daily partition.
