-- migrate:up
-- Serving by scenario, spatial part (see 20261018120100_serving_scenarios). zone_map and ranked_sites show the
-- official scenario (through zone_card, feat.latest_score and feat.score_component) and keep their columns.
--
-- Contract notes: zone_map and ranked_sites gain scenario_id; ranked_sites gains score_flags (missing-input
-- flags such as no_station_data) and its breakdown components gain `flag`.

create or replace view serving.zone_map as
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
       coalesce(zc.labels, '{}')                         as labels,
       zc.scenario_id
from ref.entity e
join ref.entity_geometry g on g.entity_id = e.entity_id and g.sensitivity = 'public'
left join serving.zone_card zc on zc.entity_id = e.entity_id
where e.kind = 'zone' and e.sensitivity = 'public';

create or replace view serving.ranked_sites as
with sites as (
    select e.entity_id, e.name, e.kind, e.parent_id, e.attributes, g.geom,
           coalesce(s.breakdown ->> 'parent_zone', e.parent_id) as zone_id,
           s.value, s.period_start, s.period_end, s.breakdown, s.run_id, s.computed_at, s.scenario_id
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
               'unverified_values', sc.unverified_values, 'flag', sc.flag)
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
       zc.revenue_unverified_values,
       s.scenario_id,
       coalesce(array(select jsonb_array_elements_text(s.breakdown -> 'flags')), '{}') as score_flags
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
           coalesce((s.breakdown ->> 'unverified_values')::integer, 0) > 0
           or coalesce(zc.revenue_unverified_values, 0) > 0 as unverified
) p;

grant select on all tables in schema serving to anon, authenticated, scanner_analyst;

-- migrate:down
-- zone_map and ranked_sites as before scenarios (20261018090100, 20261018110000).
drop view if exists serving.ranked_sites;
drop view if exists serving.zone_map;

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
           coalesce((s.breakdown ->> 'unverified_values')::integer, 0) > 0
           or coalesce(zc.revenue_unverified_values, 0) > 0 as unverified
) p;

grant select on all tables in schema serving to anon, authenticated, scanner_analyst;
