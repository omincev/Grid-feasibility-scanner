# ADR 0001: Time is UTC, intervals are half-open

Status: accepted, 2026-10-17.

## Context

Sources disagree about time. Energi Data Service gives both `TimeUTC` and `TimeDK` without a zone suffix, ENTSO-E uses period start and resolution in UTC, some national portals label hour-ending local time, PDFs say "hour 3". Danish local days have 23, 24 or 25 hours. A pipeline that mixes these produces silent one-hour shifts that corrupt spreads and backtests.

## Decision

Every stored time is a `timestamptz` in UTC. Every value covers a half-open interval `[interval_start, interval_end)`, stored explicitly in both columns, so the resolution of a row never has to be inferred. Adapters convert at the boundary and record which source field they used. When a source offers a UTC field, adapters use it and ignore the local one. When a source only gives local time, the adapter converts with `zoneinfo` using the zone's IANA timezone from `registry/countries/<cc>.yaml`, and the manifest states whether the source labels interval start or interval end.

Local time exists only in display code and in the function that computes expected intervals per local day (`scanner.core.time.expected_intervals`). A local day is the unit for daily partitions and for completeness checks, because that is how markets publish.

## Consequences

Completeness checks know that a Danish day has 92, 96 or 100 quarter-hours. Joins across sources line up by construction. Display code must convert back for users. Any adapter that cannot determine the time convention of its source is blocked until a human decides, through a bead labelled `human`.
