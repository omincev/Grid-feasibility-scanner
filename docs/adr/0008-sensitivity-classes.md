# ADR 0008: Sensitivity classes, enforced in the database

Status: accepted, 2026-10-17.

## Context

The platform will hold public market data, licence-restricted data, data received under NDA, customer telemetry and personal data about landowners (GDPR). A score derived from NDA data is NDA data. The factory and research agents must never see anything beyond public data, because their context can leak through prompts, logs, commits and model providers.

## Decision

Five ordered classes, stored as the enum `core.sensitivity`: `public`, `licensed`, `nda`, `customer`, `personal`. Every series, fact, claim, estimate, feature value, score and entity carries a class. Derived rows take the highest class of their inputs; the helper `scanner.core.sensitivity.combine` computes it and every asset uses it.

Postgres row-level security enforces access. The anonymous role used by the public front end reads only `serving` views, which filter to `public`. Analysts read up to `nda` within their organisation. `customer` rows are visible to the owning organisation and named Vorkon staff. `personal` rows are visible only to the roles that need them, and support deletion on request.

Factory and research agents run with no database credentials. Fixtures and claims in the main repository are `public` only.

## Consequences

Every new table needs a sensitivity column and an RLS policy before it ships (CI check in `make contracts` on migrations). Some features cannot be shown on the public map once they include customer data; they are computed twice, once public-only.
