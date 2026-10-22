# ADR 0005: The runtime runs code; the factory writes code

Status: accepted, 2026-10-17.

## Context

We want LLM agents to build and maintain most of the ingestion code, and we want data that updates every day without anyone present. Mixing the two (agents fetching and writing data directly) makes the data non-reproducible, exposes credentials to agents, and lets prompt injection from fetched content reach the database.

## Decision

The runtime is deterministic code on `main`, scheduled by Dagster. The factory is a separate process (beads queue, supervisor, sandboxed workers) whose only output is pull requests. A human merges; CI deploys.

The runtime sends work back to the factory through `ops.bead_outbox` when a hard check fails, a soft check persists, a watched document changes, or the PDF parser falls back to the LLM. Each outbox entry links the raw capture by URI and never copies sensitive content. The supervisor drains the outbox into `bd`.

The single exception to "no LLM in the runtime" is the PDF extraction fallback in ADR 0009, whose output is quarantined until approved.

## Consequences

Data quality never depends on an agent run succeeding. Agents never hold production credentials. Every change to how data is produced is a reviewed commit.
