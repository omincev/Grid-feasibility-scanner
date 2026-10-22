-- migrate:up
-- Capacity map (WP1.5, F7). The runtime writes machine-loaded entities and their points
-- (scanner.runtime.capacity_map), so the ingest role needs to write ref.entity and ref.entity_geometry;
-- the RLS policy ingest_all from 0900 already covers both tables.
grant insert, update on ref.entity, ref.entity_geometry to scanner_ingest;

-- serving.substation_map, same columns plus two at the end:
-- * capacity: facts whose codebook entry is in a capacity.* class (core.fact.attribute holds the entry
--   ID), keyed by entry ID; the latest per attribute is the one with the latest valid_from, then the
--   latest known_at. Each value also carries valid_from (the publication date) and run_id.
-- * attributes: the entity's attributes (source station code, voltage, zone, municipality, DSO).
-- * f7: statistic -> value of the station's rows in the latest F7 run (scanner.features.substation).
-- Stations a capacity-map run no longer lists (attributes.listed = false) are left out.
create or replace view serving.substation_map as
with latest_fact as (
    select distinct on (f.entity_id, f.attribute)
           f.entity_id, f.attribute, f.value_num, f.value_text, f.value_json, f.unit, f.valid_from,
           f.known_at, f.run_id
    from core.fact f
    left join ref.codebook_entry ce on ce.entry_id = f.attribute
    where f.sensitivity = 'public'
      and (ce.equivalence_class like 'capacity.%' or f.attribute like 'capacity.%')
      and (f.valid_from is null or f.valid_from <= now())
    order by f.entity_id, f.attribute, f.valid_from desc nulls last, f.known_at desc, f.fact_id desc
),
f7_run as (
    select r.run_id
    from ops.compute_run r
    where r.kind = 'feature' and r.name = 'f7_substation_saturation' and r.status in ('ok', 'partial')
    order by r.started_at desc
    limit 1
)
select e.entity_id,
       e.name,
       e.parent_id                        as zone_or_area_id,
       st_asgeojson(g.geom)::jsonb        as geometry,
       cap.capacity,
       e.attributes,
       f7.f7
from ref.entity e
join ref.entity_geometry g on g.entity_id = e.entity_id and g.sensitivity = 'public'
left join lateral (
    select jsonb_object_agg(lf.attribute, jsonb_build_object(
               'num', lf.value_num, 'text', lf.value_text, 'json', lf.value_json, 'unit', lf.unit,
               'known_at', lf.known_at, 'valid_from', lf.valid_from, 'run_id', lf.run_id)) as capacity
    from latest_fact lf
    where lf.entity_id = e.entity_id
) cap on true
left join lateral (
    select jsonb_object_agg(fv.statistic, fv.value) as f7
    from feat.feature_value fv
    join f7_run r on r.run_id = fv.run_id
    where fv.feature_key = 'f7_substation_saturation' and fv.entity_id = e.entity_id and fv.sensitivity = 'public'
) f7 on true
where e.kind in ('substation', 'transformer')
  and e.sensitivity = 'public'
  and coalesce((e.attributes ->> 'listed')::boolean, true);

-- migrate:down
drop view if exists serving.substation_map;
create view serving.substation_map as
with latest_fact as (
    select distinct on (f.entity_id, f.attribute)
           f.entity_id, f.attribute, f.value_num, f.value_text, f.value_json, f.unit, f.known_at
    from core.fact f
    where f.sensitivity = 'public'
      and f.attribute like 'capacity.%'
    order by f.entity_id, f.attribute, f.known_at desc
)
select e.entity_id,
       e.name,
       e.parent_id                        as zone_or_area_id,
       st_asgeojson(g.geom)::jsonb        as geometry,
       jsonb_object_agg(lf.attribute, jsonb_build_object(
           'num', lf.value_num, 'text', lf.value_text, 'json', lf.value_json,
           'unit', lf.unit, 'known_at', lf.known_at)) filter (where lf.attribute is not null) as capacity
from ref.entity e
join ref.entity_geometry g on g.entity_id = e.entity_id and g.sensitivity = 'public'
left join latest_fact lf on lf.entity_id = e.entity_id
where e.kind in ('substation', 'transformer') and e.sensitivity = 'public'
group by e.entity_id, e.name, e.parent_id, g.geom;
revoke insert, update on ref.entity, ref.entity_geometry from scanner_ingest;
