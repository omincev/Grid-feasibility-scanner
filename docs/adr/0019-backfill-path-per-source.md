# ADR 0019: Every source ships with a backfill path that respects its limits

Status: accepted, 2026-10-18, owner decision.

## Context

Daily partitions make one request per day per dataset. Some sources forbid that pace (Energi Data Service's retired `Elspotprices` allows about one call per five minutes; ENTSO-E prices for 15 zones would take hours), and a new server or a long outage needs history loaded unattended.

## Decision

A source is not scheduled until `scanner.cli ingest --dataset <name> --from <history start>` loads its full history within the source's rate limits and query-span limits, using wide requests or response reuse where needed (31-day requests for `Elspotprices`; wide ENTSO-E responses reused across past days, while recent days always fetch fresh). The dossier's "Backfill" section records history, limits, chunking, request count, expected duration and the command. `scanner.cli catch-up` and an hourly sensor find days without a full-day run and backfill them through the same path; current-state sources are reported as gaps. Dagster partition backfills are not used for history.

## Consequences

Adapters carry a little more logic (reuse, chunking) and a test that shows it. Recovery after downtime is automatic for sources with history; sources without it depend on continuous snapshots (ADR 0003).
