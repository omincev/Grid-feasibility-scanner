# Source dossier: <name>

Status: draft | approved. Country: <cc>. Owner: <domain owner>. Last verified: <date>.

## What it is

Who publishes it, what it covers, why we need it (which features, estimates or valuations use it).

## Access

- Method: REST API | ArcGIS FeatureServer | Power BI embed | file download | PDF | browser-only | request by email or phone
- Endpoint or URL:
- Authentication: none | token (runtime secret name) | account
- Rate limits and polite-use rules:
- How it was found (for browser-only sources: the network requests inspected):

## Datasets

One subsection per dataset: name, fields with the source's own definitions quoted, resolution, time convention (UTC field? interval start or end?), update cadence and publication time, history available, known gaps, known regime changes.

## Backfill

Every source needs a backfill path before it is scheduled. Record: history available per dataset; rate limits and what happens when they are exceeded (429, `Retry-After`, bans); maximum time span per request and page size; the chunking or response reuse the adapter uses; the resulting request count and expected duration for a full backfill; and the exact command (`scanner.cli ingest --dataset <name> --from <date>`). Sources without retrievable history (current-state maps, overwritten forecasts) say so, and are snapshotted from day one instead.

## Decisions

A short list of the normalisation decisions made for this source, each with where it is implemented: time field and its time zone, resolution (native, per period), units and sign conventions kept as published or checked, how nulls, gaps, duplicates and revisions are handled, which variant of a measure is ingested and which is not, and anything that needs owner sign-off. The ingest assets show this section in Dagster (`scanner.assets.rules`), so keep it current when a decision changes.

## Licence and terms

Licence name and link, attribution required, redistribution allowed in a paid product, terms-of-use URL to watch.

## Measured or estimated

Which quantities are published directly and which we must estimate from this source.

## Sensitivity

Default class for data from this source.

## Codebook entries needed

List of entries to create in `codebook/<cc>/`.

## Risks

Format changes seen before, reliability, political or legal risk of withdrawal.
