# ADR 0015: One docker compose stack, locally and on a single VPS

Status: accepted, 2026-10-18, owner decision. Amends ADR 0010; resolves open decisions D6 and D7 for now.

## Context

ADR 0010 put Postgres on Supabase and left object storage (D6) and Dagster hosting (D7) open. The measured footprint is small: about 1 GB of database, about 1 GB of RAM for Dagster at rest, a few GB of raw captures a year after the polling-window change. Running every component the same way locally and in production keeps moves cheap.

## Decision

The runtime is one docker compose stack: Postgres with PostGIS, PostgREST serving the `serving` schema to `anon`, Dagster (webserver, daemon, code locations) and the raw store. Locally the raw store is a directory; in production the stack runs on one Hetzner Cloud VPS (CX33, EU) with the raw store on a local MinIO behind the S3 API, PostgREST behind HTTPS (Caddy), and the Dagster and MinIO consoles reachable only privately (Tailscale or an SSH tunnel). Every endpoint and credential is an environment variable; raw references are stored relative to the store (`raw://...`). Images are built and deployed only on a manual trigger. The SQL and roles stay Supabase-compatible (`anon`, `authenticated`, `authenticator`).

## Consequences

Moving the database to Supabase or the raw store to Cloudflare R2 later is a restore or mirror plus an environment change. One machine is a single point of failure: daily backups with a weekly restore check, an optional off-site copy, and the catch-up job (ADR 0019) bound the loss; current-state snapshots missed during downtime cannot be recovered. Operating the server (patching, disk, backups) is ours. Runbook: `infra/README.md`.
