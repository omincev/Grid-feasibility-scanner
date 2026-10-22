-- migrate:up
-- Spatial serving views (PostGIS).

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

create view serving.ranked_sites as
with latest_score as (
    select distinct on (s.entity_id) s.entity_id, s.value, s.breakdown, s.run_id
    from feat.score s
    where s.score_kind = 'congestion' and s.sensitivity = 'public'
    order by s.entity_id, s.computed_at desc
)
select e.entity_id,
       e.name,
       e.kind,
       e.parent_id,
       st_asgeojson(st_pointonsurface(g.geom))::jsonb as point,
       ls.value as congestion_score,
       ls.breakdown,
       rr.low   as revenue_low_eur_per_mw_year,
       rr.high  as revenue_high_eur_per_mw_year,
       rr.labels as revenue_labels,
       rank() over (order by ls.value desc nulls last) as rank
from ref.entity e
join latest_score ls on ls.entity_id = e.entity_id
left join ref.entity_geometry g on g.entity_id = e.entity_id and g.sensitivity = 'public'
left join serving.return_ranges rr
       on rr.entity_id = e.entity_id and rr.asset_type = 'bess' and rr.metric = 'revenue_eur_per_mw_year'
where e.kind in ('substation', 'municipality', 'site') and e.sensitivity = 'public';

-- migrate:down
drop view if exists serving.ranked_sites;
drop view if exists serving.substation_map;
