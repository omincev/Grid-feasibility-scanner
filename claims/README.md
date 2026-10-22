# Claims

Figures extracted from documents (regulator reports, operator publications, PDFs), with the evidence needed to check them. One file per document family and country: `claims/<cc>/<family>.yaml`. Schema: `scanner.core.registry.ClaimsFile`. Only `public` claims live here; others go in the restricted repository.

```yaml
document:
  id: it.arera.relazione_301_2026
  source: arera            # source key, as in ref.source
  url: https://...
  title: ARERA Relazione 301/2026/I/EFR
  fetched_at: 2026-10-18T08:00:00Z   # when the capture was taken, UTC
  sha256: "..."            # of the captured PDF in the raw store
  raw_uri: raw://<source>/<dataset>/...   # store-relative reference of the capture (scanner.core.raw_store)
  content_type: application/pdf
claims:
  - id: it.arera.301_2026.wind_curtailment_2025_gwh
    attribute: curtailment.wind.energy_gwh
    entity: it.country.it
    value: 640
    unit: GWh
    valid_from: 2025-01-01
    valid_to: 2026-01-01
    quote: "..."            # verbatim text containing the value
    page: 0
    location: null          # table and cell, or bounding box
    method: manual          # parser | llm | manual
    model: null
    prompt_hash: null
    status: pending_review  # approved once the domain owner signs off
    reviewed_by: null
```

The citation check (`scanner.extraction.citations`) requires `quote` to appear in the page text and the value to be parseable from it.

`scanner.cli load-curated` loads each file into `core.source_document` and `core.claim` with the `status` written here. A numeric `value` becomes `value_num`, text becomes `value_text`. `llm` claims need `model` and `prompt_hash`; `approved` claims need `reviewed_by`. Optional fields (`valid_from`, `valid_to`, `quote`, `page`, `location`, `model`, `prompt_hash`, `reviewed_by`, `reviewed_on`) default to null; `sensitivity` defaults to `public`, the only class allowed in this repository.
