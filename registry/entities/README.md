# Entities and aliases

Bidding zones are declared in `registry/countries/<cc>.yaml` and loaded as entities from there. Everything else (substations, transformers, lines, DSO areas, municipalities, plants, sites) is declared here, one folder per country, per ADR 0004.

`<cc>/entities.yaml`:

```yaml
entities:
  - id: dk.substation.energinet.idomlund      # <cc>.<kind>.<slug>
    kind: substation
    parent: dk.zone.dk1
    name: Idomlund
    attributes: {voltage_kv: [400, 150]}
    geometry_blob: null                        # must stay null: geometries live in registry/geometries
    geometry_wkt: null
```

Schemas: `scanner.core.registry.EntitiesFile` and `AliasesFile`. Optional entity fields: `eic`, `sensitivity` (default `public`; anything else belongs in the restricted repository). Geometries are GeoJSON FeatureCollections in `registry/geometries/<cc>/<name>.geojson` (EPSG:4326, `properties.entity_id` per feature) with a sibling `<name>.meta.yaml` (`scanner.core.registry.GeometryMeta`); `scanner.cli load-curated` loads them into `ref.entity_geometry`.

`<cc>/aliases.yaml`:

```yaml
aliases:
  - entity_id: dk.substation.energinet.idomlund
    source: energinet_capacity_map
    source_id: "..."
    source_name: "Idomlund 400 kV"
    approved_by: misi
    approved_on: 2026-10-20
    evidence: {distance_m: 12, name_similarity: 0.94}
```

Agents may add entities and propose aliases in a PR with evidence; every alias needs `approved_by` from a human (H6). Unmatched source entities stay unmatched.

Entities a source defines and the runtime loads (capacity-map stations and DSO licence areas, connection-queue regions) are machine data: they are written to the database with `source_path` `runtime:<source>` and are not declared here.
