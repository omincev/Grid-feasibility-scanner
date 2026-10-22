# ENTSO-E fixtures

Public data only. Every file has a `.meta.json` sidecar with its provenance and SHA-256.

- `postman__*.xml`: example responses copied verbatim from ENTSO-E's official API documentation (the "Transparency Platform Restful API" Postman collection, published 2026-01-16). Not recorded by us; they pin down the document structure (A44 with curveType A03, A65, A75 with generation and consumption series, A11).
- `day_ahead_prices__no2__2026-08-08__duplicate_timeseries`: real NO2 A44 response recorded by the orchestrator; two TimeSeries identical but for mRID.
- `<case>.xml`: real responses recorded by `scripts/record_entsoe_fixtures.py`, which the orchestrator runs with the security token. The token travels in a request header and is checked absent from every file before writing.
