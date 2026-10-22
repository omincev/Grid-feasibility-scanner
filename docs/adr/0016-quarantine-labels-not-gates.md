# ADR 0016: Quarantined data is used and labelled; only a person excludes it

Status: accepted, 2026-10-18, owner decision. Amends ADR 0013.

## Context

ADR 0013 lets soft deviations land with `quality = 'quarantined'` "until a person clears it or the baseline catches up", and the readers took that as "excluded until cleared". The first real backfill quarantined genuine market events (a 911 EUR/MWh spike, a -480 EUR/MWh price), exactly the hours flexible assets earn on; excluding them biased every feature and broke every 365-day valuation window.

## Decision

A quarantined row is usable by every reader and labelled unverified. A person reviews the ingest run (`scanner.cli quarantine list | decide | accept-corroborated`): accept clears the label (verified), reject excludes the run's rows (a genuine data error). Readers derive this from `core.observation_effective`; outputs record how many unverified values they used. A soft deviation quarantines only the rows of the fingerprinted partition day, and only a decision clears a label, never a later clean run.

## Consequences

Real events count from the moment they arrive; data errors that the fingerprint catches stay in results until someone rejects them, so review matters more for sources without an independent cross-check. The UI shows unverified inputs instead of hiding them.
