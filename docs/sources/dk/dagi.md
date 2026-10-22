# DAGI municipality boundaries (Klimadatastyrelsen, via Datafordeler)

Status: research from public docs on 2026-10-18; the live path is unverified until the first run.

- Use: municipality polygons (98 kommuner) for `registry/geometries/dk/municipalities.geojson` and the dissolved zones.
- Service: Datafordeler GraphQL, `POST https://graphql.datafordeler.dk/DAGI/v2`, entity `DAGI_Kommuneinddeling`
  (key attribute `kommunekode`, id `id_lokalId`). Geometry is a sub-selection `{ crs wkt }`; native CRS EPSG:25832.
  The field names `geometri` and the endpoint version (`v2`; docs also mention v1, v3) are assumed, not confirmed.
  The script prints the entity's field list on a GraphQL error; override with `--dagi-geometry-field` / `--dagi-url`.
- Auth: API key as query parameter `?apiKey=...` (the only form the docs show; no header form documented).
  New keys take about 15 minutes to become active. Setting: `DATAFORDELER_API_KEY` (`SecretStr`), never logged or written.
- Paging: cursor based, `first` (default 100, max 1000) and `after: endCursor`; the script uses 5 per page because
  full-resolution polygons are large. Without `virkningstid` and `registreringstid` the service returns every
  bitemporal version, so the script sets both to now.
- Other services: WFS and file download exist for the new entities (from July 2026); the old REST, WFS geoserver and
  WMS get no further updates and the old REST is retired 2027-01-15. Scaled variants (`_250000`, ...) are not available.
- Licence: Klimadatastyrelsen holds the rights to the free geographic data; terms at
  <https://datafordeler.dk/vejledning/brugervilkaar/kds-geografiske-data/>. Attribution wording is unverified; the meta
  files record a proposed line, to be confirmed by a human.
- Note: the SDFI-adjusted boundaries have no formal legal status in themselves (per the DAGI documentation).

Run (orchestrator, key in the environment or `.env`):

    uv run python scripts/build_geometries.py --source dagi

Fallback without a key: `--source gisco` (Eurostat GISCO 1:1M, coarse).
