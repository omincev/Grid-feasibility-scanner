# Codebook

Every source field that becomes a series or fact is defined here (ADR 0012). One file per source dataset: `codebook/<cc>/<source>.<dataset>.yaml`. Schema: `scanner.core.codebook.CodebookFile`. Equivalence classes: `classes.yaml`.

An entry records:

- `id`: `<cc>.<source_short>.<dataset>.<field>[.<dimension value>]`
- `field` and `dimensions`: which column, and which values of dimension columns (for example `ForecastType: Solar`) select this entry
- `equivalence_class`: the class it belongs to; assigning it is a claim the domain owner signs off
- `unit`, `sign_convention`, `resample` (`sum`, `mean`, `first`, `last`, `none`)
- `kind`: `actual`, `forecast`, `settlement`, `capacity`
- `issued_at`: for forecasts, how to determine issue time (`field`, `fixed_local_time`, `offset`)
- `definition.source_quote`: the source's definition, verbatim, with `source_url`; null until pasted from the source
- `definition.ours`: our definition in plain words
- `notes`: discrepancies, regime changes, caveats

File-level `status` is `draft` until the owner sets `verified_by` and `verified_on` and changes it to `approved`. Features only consume approved entries in production; drafts are allowed in development and in the MVP with a warning.

Fields a source publishes that we deliberately do not ingest (for example DKK prices computed from EUR) are listed under `not_ingested` with the reason.
