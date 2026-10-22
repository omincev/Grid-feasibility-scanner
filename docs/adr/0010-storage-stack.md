# ADR 0010: Postgres on Supabase with PostGIS, object storage, Dagster, DuckDB for compute

Status: accepted, 2026-10-17. Amended by ADR 0015 (one docker compose stack; Supabase and R2 remain later options).

## Context

The store must accept continuous writes from scheduled jobs while a front end, notebooks and later an API read from it. Public data volumes are small (a 15-minute series is about 35,000 points a year; 500 series over five years is under 100 million rows). The Lovable front end connects natively to Supabase. Sparse facts, entity crosswalks and Layer 2 data need transactions, constraints, joins and row-level security.

## Decision

Postgres 16 on Supabase is the system of record, with PostGIS for geometry. Tables use plain Postgres; `core.observation` is unpartitioned with a BRIN index on `interval_start` until a measured need appears, then monthly range partitions. TimescaleDB is not used: it is not needed at our volume, and as far as we know Supabase no longer offers it on Postgres 17 (verify before revisiting).

Raw captures live in S3-compatible object storage behind `fsspec` (provider: open decision D6). DuckDB is a compute engine for backtests and heavy feature runs, reading from Postgres through its postgres extension or from Parquet exports. It is not a store.

Dagster OSS orchestrates the runtime, with its run storage in a separate Postgres schema.

A separate high-frequency store (ClickHouse or TimescaleDB) is added only when customer telemetry at second-level resolution arrives (Phase 7), and only for that stream.

## Consequences

One database to operate, back up and secure. Lovable integration on 21 October needs no extra service. Heavy analytics stay off the database the front end depends on.
