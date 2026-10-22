-- migrate:up
-- Serving contract for the hackathon UI (WP1.10, docs/ui-contract.md), spatial part (PostGIS).
-- Geometries are GeoJSON in EPSG:4326 with 6 decimals (about 0.1 m); nothing is simplified.
--
-- Contract notes:
-- * New: serving.zone_map.
-- * serving.substation_map keeps entity_id, name, zone_or_area_id, geometry, capacity, attributes and f7, and
--   adds F7 free MW and status per direction, limited-access ranges, publication date and run IDs. MW colour
--   thresholds are not set by the owners yet, so only the factual status is served.
-- * serving.ranked_sites keeps entity_id, name, kind, parent_id, point, congestion_score, revenue_low/high,
--   revenue_labels and rank; breakdown is now a list of components with titles (was the raw score JSON).

-- Zone polygons with the zone card's headline values and flags.
create view serving.zone_map as
select e.entity_id,
       e.name,
       e.attributes ->> 'short'                          as short_name,
       e.country_code,
       st_asgeojson(g.geom, 6)::jsonb                    as geometry,
       zc.congestion_score,
       zc.period_start,
       zc.period_end,
       zc.score_run_id,
       coalesce(zc.best_fit_asset_types, '{}')           as best_fit_asset_types,
       zc.revenue_low_eur_per_mw_year,
       zc.revenue_high_eur_per_mw_year,
       zc.revenue_labels,
       zc.revenue_label_text,
       zc.revenue_run_id,
       coalesce(zc.proposed_assumptions, false)          as proposed_assumptions,
       zc.unverified_data,
       coalesce(zc.stale_source, false)                  as stale_source,
       coalesce(zc.labels, '{}')                         as labels
from ref.entity e
join ref.entity_geometry g on g.entity_id = e.entity_id and g.sensitivity = 'public'
left join serving.zone_card zc on zc.entity_id = e.entity_id
where e.kind = 'zone' and e.sensitivity = 'public';

-- Substations as points. free_mw_<direction> is the station's free MW as F7 reads it from the map (the lowest
-- of the published values that apply; null when not published); status_<direction> only says whether that
-- figure is 0, above 0 or missing. The current fact per attribute is the one with the latest valid_from,
-- then the latest known_at (as in 20261018060000).
drop view if exists serving.substation_map;
create view serving.substation_map as
with latest_fact as (
    select distinct on (f.entity_id, f.attribute)
           f.entity_id, f.attribute, f.value_num, f.value_text, f.value_json, f.unit, f.valid_from,
           f.known_at, f.run_id, ce.equivalence_class, ce.dimensions
    from core.fact f
    left join ref.codebook_entry ce on ce.entry_id = f.attribute
    where f.sensitivity = 'public'
      and (ce.equivalence_class like 'capacity.%' or f.attribute like 'capacity.%')
      and (f.valid_from is null or f.valid_from <= now())
    order by f.entity_id, f.attribute, f.valid_from desc nulls last, f.known_at desc, f.fact_id desc
),
station_facts as (
    select lf.entity_id,
           jsonb_object_agg(lf.attribute, jsonb_build_object(
               'num', lf.value_num, 'text', lf.value_text, 'json', lf.value_json, 'unit', lf.unit,
               'known_at', lf.known_at, 'valid_from', lf.valid_from, 'run_id', lf.run_id)) as capacity,
           max(lf.value_num) filter (where lf.equivalence_class = 'capacity.free_mw_limited.consumption'
                                       and lf.dimensions ->> 'bound' = 'lower') as limited_consumption_mw_low,
           max(lf.value_num) filter (where lf.equivalence_class = 'capacity.free_mw_limited.consumption'
                                       and lf.dimensions ->> 'bound' = 'upper') as limited_consumption_mw_high,
           max(lf.value_num) filter (where lf.equivalence_class = 'capacity.free_mw_limited.production'
                                       and lf.dimensions ->> 'technology' = 'solar'
                                       and lf.dimensions ->> 'bound' = 'lower') as limited_solar_mw_low,
           max(lf.value_num) filter (where lf.equivalence_class = 'capacity.free_mw_limited.production'
                                       and lf.dimensions ->> 'technology' = 'solar'
                                       and lf.dimensions ->> 'bound' = 'upper') as limited_solar_mw_high,
           max(lf.value_num) filter (where lf.equivalence_class = 'capacity.free_mw_limited.production'
                                       and lf.dimensions ->> 'technology' = 'wind'
                                       and lf.dimensions ->> 'bound' = 'lower') as limited_wind_mw_low,
           max(lf.value_num) filter (where lf.equivalence_class = 'capacity.free_mw_limited.production'
                                       and lf.dimensions ->> 'technology' = 'wind'
                                       and lf.dimensions ->> 'bound' = 'upper') as limited_wind_mw_high,
           (max(lf.valid_from) at time zone 'UTC')::date as publication_date,
           array_agg(distinct lf.run_id) filter (where lf.run_id is not null) as capacity_run_ids
    from latest_fact lf
    group by lf.entity_id
),
f7 as (
    select f.entity_id,
           jsonb_object_agg(f.statistic, f.value) as f7,
           max(f.value) filter (where f.statistic = 'free_mw_production') as free_mw_production,
           max(f.value) filter (where f.statistic = 'free_mw_consumption') as free_mw_consumption,
           max(f.value) filter (where f.statistic = 'rank_in_zone_production') as rank_in_zone_production,
           max(f.value) filter (where f.statistic = 'rank_in_zone_consumption') as rank_in_zone_consumption,
           min(f.run_id::text)::uuid as run_id
    from feat.f7_latest f
    group by f.entity_id
)
select e.entity_id,
       e.name,
       e.parent_id                                    as zone_or_area_id,
       st_asgeojson(g.geom, 6)::jsonb                 as geometry,
       sf.capacity,
       e.attributes,
       f7.f7,
       e.attributes ->> 'municipality'                as municipality_id,
       e.attributes ->> 'dso'                         as dso_area_id,
       case when e.attributes ? 'dso' then 'distribution' else 'transmission' end as network_level,
       e.attributes ->> 'voltage_kv'                  as voltage_kv,
       f7.free_mw_production,
       f7.free_mw_consumption,
       case when f7.free_mw_production > 0 then 'free_capacity'
            when f7.free_mw_production = 0 then 'no_free_capacity'
            else 'unknown' end                        as status_production,
       case when f7.free_mw_consumption > 0 then 'free_capacity'
            when f7.free_mw_consumption = 0 then 'no_free_capacity'
            else 'unknown' end                        as status_consumption,
       sf.limited_consumption_mw_low,
       sf.limited_consumption_mw_high,
       sf.limited_solar_mw_low,
       sf.limited_solar_mw_high,
       sf.limited_wind_mw_low,
       sf.limited_wind_mw_high,
       coalesce(sf.limited_consumption_mw_low > sf.limited_consumption_mw_high, false)
           or coalesce(sf.limited_solar_mw_low > sf.limited_solar_mw_high, false)
           or coalesce(sf.limited_wind_mw_low > sf.limited_wind_mw_high, false) as limited_range_inverted,
       f7.rank_in_zone_production,
       f7.rank_in_zone_consumption,
       'current snapshot'::text                       as f7_caveat,
       f7.run_id                                      as f7_run_id,
       sf.publication_date,
       sf.capacity_run_ids
from ref.entity e
join ref.entity_geometry g on g.entity_id = e.entity_id and g.sensitivity = 'public'
left join station_facts sf on sf.entity_id = e.entity_id
left join f7 on f7.entity_id = e.entity_id
where e.kind in ('substation', 'transformer')
  and e.sensitivity = 'public'
  and coalesce((e.attributes ->> 'listed')::boolean, true);

-- Site cards: scored entities below zone level (today the municipalities), ranked by congestion score.
-- The revenue range and stale come from the parent zone's card (sites inherit its day-based inputs);
-- unverified data from the site's own score and the zone's range.
drop view if exists serving.ranked_sites;
create view serving.ranked_sites as
with sites as (
    select e.entity_id, e.name, e.kind, e.parent_id, e.attributes, g.geom,
           coalesce(s.breakdown ->> 'parent_zone', e.parent_id) as zone_id,
           s.value, s.period_start, s.period_end, s.breakdown, s.run_id, s.computed_at
    from ref.entity e
    join feat.latest_score s on s.entity_id = e.entity_id
    left join ref.entity_geometry g on g.entity_id = e.entity_id and g.sensitivity = 'public'
    where e.kind in ('substation', 'municipality', 'site') and e.sensitivity = 'public'
),
f7 as (
    select f.entity_id,
           max(f.value) filter (where f.statistic = 'stations') as stations,
           max(f.value) filter (where f.statistic = 'share_stations_no_free_production') as share_no_free_production,
           max(f.value) filter (where f.statistic = 'share_stations_no_free_consumption') as share_no_free_consumption,
           max(f.value) filter (where f.statistic = 'free_mw_production_sum') as free_mw_production_sum,
           max(f.value) filter (where f.statistic = 'free_mw_consumption_sum') as free_mw_consumption_sum,
           min(f.run_id::text)::uuid as run_id
    from feat.f7_latest f
    group by f.entity_id
),
components as (
    select sc.entity_id,
           jsonb_agg(jsonb_build_object(
               'feature_key', sc.feature_key, 'weight_key', sc.weight_key, 'statistic', sc.statistic,
               'title', sc.title, 'unit', sc.unit, 'caveat', sc.caveat, 'raw_value', sc.raw_value,
               'percentile', sc.percentile, 'weight', sc.weight, 'contribution', sc.contribution,
               'inherited_from', sc.inherited_from, 'feature_run_id', sc.feature_run_id,
               'unverified_values', sc.unverified_values)
               order by sc.display_order, sc.feature_key) as breakdown
    from feat.score_component sc
    group by sc.entity_id
)
select s.entity_id,
       s.name,
       s.kind,
       s.parent_id,
       st_asgeojson(st_pointonsurface(s.geom), 6)::jsonb                     as point,
       s.value                                                               as congestion_score,
       c.breakdown,
       zc.revenue_low_eur_per_mw_year,
       zc.revenue_high_eur_per_mw_year,
       zc.revenue_labels,
       rank() over (partition by s.kind order by s.value desc)               as rank,
       s.zone_id,
       z.name                                                                as zone_name,
       rank() over (partition by s.kind, s.zone_id order by s.value desc)    as rank_in_zone,
       count(*) over (partition by s.kind)                                   as site_count,
       count(*) over (partition by s.kind, s.zone_id)                        as site_count_in_zone,
       s.period_start,
       s.period_end,
       s.run_id                                                              as score_run_id,
       s.computed_at                                                         as score_computed_at,
       coalesce(array(select jsonb_array_elements_text(s.breakdown -> 'labels')), '{}') as score_labels,
       coalesce(o.best_fit_asset_types, '{}')                                as best_fit_asset_types,
       o.asset_types,
       o.run_id                                                              as opportunity_run_id,
       f7.stations                                                           as f7_stations,
       f7.share_no_free_production                                           as f7_share_no_free_production,
       f7.share_no_free_consumption                                          as f7_share_no_free_consumption,
       f7.free_mw_production_sum                                             as f7_free_mw_production_sum,
       f7.free_mw_consumption_sum                                            as f7_free_mw_consumption_sum,
       'current snapshot'::text                                              as f7_caveat,
       f7.run_id                                                             as f7_run_id,
       zc.entity_id                                                          as revenue_entity_id,
       zc.revenue_label_text,
       zc.revenue_run_id,
       b.benchmark_id is not null                                            as is_benchmark,
       b.benchmark_id,
       p.proposed                                                            as proposed_assumptions,
       p.unverified                                                          as unverified_data,
       coalesce(zc.stale_source, false)                                      as stale_source,
       array_remove(array[
           case when p.proposed then 'proposed assumptions (not approved)' end,
           case when p.unverified then 'unverified data' end,
           case when zc.stale_source then 'stale source' end], null)         as labels,
       s.attributes,
       (s.breakdown ->> 'unverified_values')::integer                        as score_unverified_values,
       zc.revenue_unverified_values
from sites s
left join ref.entity z on z.entity_id = s.zone_id
left join components c on c.entity_id = s.entity_id
left join f7 on f7.entity_id = s.entity_id
left join feat.opportunity_summary o on o.entity_id = s.entity_id
left join serving.zone_card zc on zc.entity_id = s.zone_id
left join lateral (
    select min(bm.benchmark_id) as benchmark_id from ref.benchmark bm
    where bm.entity_id = s.entity_id and bm.sensitivity = 'public'
) b on true
cross join lateral (
    select coalesce((s.breakdown ->> 'proposed_assumptions')::boolean, false)
           or coalesce(o.proposed_assumptions, false)
           or coalesce(zc.revenue_proposed_assumptions, false) as proposed,
           (s.breakdown ->> 'unverified_values')::integer > 0 or zc.revenue_unverified_values > 0 as unverified
) p;

grant usage on schema serving to anon, authenticated, scanner_analyst;
grant select on all tables in schema serving to anon, authenticated, scanner_analyst;

-- migrate:down
drop view if exists serving.zone_map;

-- serving.ranked_sites as in 20261017000810.
drop view if exists serving.ranked_sites;
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

-- serving.substation_map as in 20261018060000.
drop view if exists serving.substation_map;
create view serving.substation_map as
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

grant select on all tables in schema serving to anon, authenticated, scanner_analyst;
