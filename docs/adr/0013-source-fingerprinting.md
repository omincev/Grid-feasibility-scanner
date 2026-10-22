# ADR 0013: Every run is fingerprinted and compared with a baseline

Status: accepted, 2026-10-17. Amended by ADR 0016 (quarantined data is used and labelled; only a person excludes it).

## Context

Sources change without notice: datasets are retired, fields renamed, sites start returning 403, layouts shift, values drift. Energy data also has real extreme events that look like anomalies (the 2022 price spike, record negative-price hours).

## Decision

Every ingest run records a fingerprint at three levels in `ops.fingerprint`:

- transport: HTTP status, content type, response size, latency, redirects
- structure: field names and types, nesting; for browser sources a hash of the parsed DOM region plus a screenshot reference
- content: rows per period against expected intervals, null rate per field, min, max and quantiles per numeric field, arrival time against expected cadence

`scanner.core.fingerprint.compare` classifies deviations against a rolling baseline. Hard deviations (non-2xx status, schema change, parse failure, zero rows when rows are expected) block the write and file a bead. Soft deviations (distribution shift, row count off, late arrival, null-rate rise) let the data land with `quality = 'quarantined'`; serving views show it as unverified until a person clears it or the baseline catches up. Soft deviations never block, so real market events are kept.

The snapshot watcher also hashes each source's terms-of-use page; a change files a bead to the licence owner.

## Consequences

Breakage surfaces within one schedule cycle. Baselines need a warm-up period per source (14 runs by default) during which only hard checks apply.
