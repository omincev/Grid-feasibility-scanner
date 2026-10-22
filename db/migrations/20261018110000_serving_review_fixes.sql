-- Review fixes (serving):
-- * serving.opportunities and serving.return_ranges now also require the entity itself to be public, like
--   the other serving views. Columns are unchanged.
-- * unverified_data (zone_card, ranked_sites; zone_map passes it through) is false, not null, when no
--   unverified values are recorded. The counts (score_unverified_values, ...) stay null when not recorded.

-- migrate:up
create or replace view serving.opportunities as
select distinct on (o.entity_id, o.asset_type)
       o.entity_id, o.asset_type, o.matched, o.status, o.reasons, o.run_id, o.computed_at
from feat.opportunity o
join ref.entity e on e.entity_id = o.entity_id
where o.sensitivity = 'public' and e.sensitivity = 'public'
order by o.entity_id, o.asset_type, o.computed_at desc;

create or replace view serving.return_ranges as
select distinct on (r.entity_id, r.asset_type, res.metric)
       r.entity_id, r.asset_type, res.metric, res.low, res.mid, res.high, res.unit,
       r.labels, r.method, r.method_version, r.template_set_id, r.run_id, r.computed_at,
       r.valuation_run_id,
       r.price_window_start,
       r.price_window_end,
       coalesce((r.inputs ->> 'proposed_assumptions')::boolean, false) as proposed_assumptions,
       (r.inputs ->> 'days_valued')::integer                           as days_valued,
       (r.inputs ->> 'skipped_days')::integer                          as skipped_days,
       (r.inputs ->> 'complete_365_day_windows')::integer              as complete_365_day_windows,
       (r.inputs ->> 'unverified_values')::integer                     as unverified_values
from val.run r
join val.result res on res.valuation_run_id = r.valuation_run_id
join ref.entity e on e.entity_id = r.entity_id
where r.sensitivity = 'public' and res.sensitivity = 'public' and e.sensitivity = 'public'
order by r.entity_id, r.asset_type, res.metric, r.computed_at desc;

create or replace view serving.zone_card as
with zones as (
    select e.entity_id, e.name, e.attributes ->> 'short' as short_name, e.country_code, e.eic,
           s.value, s.period_start, s.period_end, s.breakdown, s.weights_set_id, s.run_id, s.computed_at
    from ref.entity e
    join feat.latest_score s on s.entity_id = e.entity_id
    where e.kind = 'zone' and e.sensitivity = 'public'
),
flags as (
    -- Datasets of the sources the zone's features read (per measure, the country's precedence) whose last
    -- successful run is older than their SLA.
    select z.entity_id,
           array(select distinct f.source || '.' || f.dataset
                 from core.preferred_series ps
                 join serving.source_freshness f on f.source = ps.source and f.dataset = ps.dataset
                 where ps.entity_id = z.entity_id and f.stale
                 order by 1) as stale_datasets
    from zones z
),
card as (
    select z.*,
           o.best_fit_asset_types, o.asset_types, o.run_id as opportunity_run_id,
           coalesce(o.proposed_assumptions, false) as opportunity_proposed,
           coalesce((z.breakdown ->> 'proposed_assumptions')::boolean, false) as score_proposed,
           (z.breakdown ->> 'unverified_values')::integer as score_unverified,
           rr.low, rr.high, rr.unit, rr.labels, rr.method, rr.price_window_start, rr.price_window_end,
           rr.days_valued, rr.skipped_days, rr.complete_365_day_windows, rr.run_id as revenue_run_id,
           rr.valuation_run_id, rr.computed_at as revenue_computed_at,
           coalesce(rr.proposed_assumptions, false) as revenue_proposed,
           rr.unverified_values as revenue_unverified,
           fl.stale_datasets
    from zones z
    join flags fl on fl.entity_id = z.entity_id
    left join feat.opportunity_summary o on o.entity_id = z.entity_id
    left join serving.return_ranges rr
           on rr.entity_id = z.entity_id and rr.asset_type = 'bess' and rr.metric = 'revenue_eur_per_mw_year'
)
select c.entity_id,
       c.name,
       c.short_name,
       c.country_code,
       c.eic,
       c.value                                                        as congestion_score,
       c.period_start,
       c.period_end,
       coalesce(array(select jsonb_array_elements_text(c.breakdown -> 'labels')), '{}') as score_labels,
       coalesce(array(select jsonb_array_elements_text(c.breakdown -> 'missing_features')), '{}')
                                                                      as missing_features,
       c.weights_set_id,
       c.run_id                                                       as score_run_id,
       c.computed_at                                                  as score_computed_at,
       c.breakdown,
       coalesce(c.best_fit_asset_types, '{}')                         as best_fit_asset_types,
       c.asset_types,
       c.opportunity_run_id,
       c.low                                                          as revenue_low_eur_per_mw_year,
       c.high                                                         as revenue_high_eur_per_mw_year,
       c.unit                                                         as revenue_unit,
       c.labels                                                       as revenue_labels,
       array_to_string(c.labels, ', ')                                as revenue_label_text,
       case when c.method = 'bess_v0_daily_lp_day_ahead' then array[
           'perfect foresight (overstates)',
           'no balancing revenue (understates)',
           'no degradation cost (overstates)',
           'no saturation (overstates as more storage enters)'] end    as revenue_known_biases,
       c.method                                                       as revenue_method,
       c.price_window_start                                           as revenue_window_start,
       c.price_window_end                                             as revenue_window_end,
       c.days_valued                                                  as revenue_days_valued,
       c.skipped_days                                                 as revenue_skipped_days,
       c.complete_365_day_windows                                     as revenue_complete_windows,
       c.revenue_run_id,
       c.valuation_run_id,
       c.revenue_computed_at,
       c.score_proposed                                               as score_proposed_assumptions,
       c.opportunity_proposed                                         as opportunity_proposed_assumptions,
       c.revenue_proposed                                             as revenue_proposed_assumptions,
       c.score_proposed or c.opportunity_proposed or c.revenue_proposed as proposed_assumptions,
       coalesce(c.score_unverified, 0) > 0 or coalesce(c.revenue_unverified, 0) > 0             as unverified_data,
       c.score_unverified                                             as score_unverified_values,
       c.revenue_unverified                                           as revenue_unverified_values,
       cardinality(c.stale_datasets) > 0                              as stale_source,
       c.stale_datasets,
       c.period_end                                                   as data_through,
       array_remove(array[
           case when c.score_proposed or c.opportunity_proposed or c.revenue_proposed
                then 'proposed assumptions (not approved)' end,
           case when coalesce(c.score_unverified, 0) > 0 or coalesce(c.revenue_unverified, 0) > 0 then 'unverified data' end,
           case when cardinality(c.stale_datasets) > 0 then 'stale source' end], null) as labels
from card c;

create or replace view serving.ranked_sites as
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

-- migrate:down
create or replace view serving.opportunities as
select distinct on (o.entity_id, o.asset_type)
       o.entity_id, o.asset_type, o.matched, o.status, o.reasons, o.run_id, o.computed_at
from feat.opportunity o
where o.sensitivity = 'public'
order by o.entity_id, o.asset_type, o.computed_at desc;

create or replace view serving.return_ranges as
select distinct on (r.entity_id, r.asset_type, res.metric)
       r.entity_id, r.asset_type, res.metric, res.low, res.mid, res.high, res.unit,
       r.labels, r.method, r.method_version, r.template_set_id, r.run_id, r.computed_at,
       r.valuation_run_id,
       r.price_window_start,
       r.price_window_end,
       coalesce((r.inputs ->> 'proposed_assumptions')::boolean, false) as proposed_assumptions,
       (r.inputs ->> 'days_valued')::integer                           as days_valued,
       (r.inputs ->> 'skipped_days')::integer                          as skipped_days,
       (r.inputs ->> 'complete_365_day_windows')::integer              as complete_365_day_windows,
       (r.inputs ->> 'unverified_values')::integer                     as unverified_values
from val.run r
join val.result res on res.valuation_run_id = r.valuation_run_id
where r.sensitivity = 'public' and res.sensitivity = 'public'
order by r.entity_id, r.asset_type, res.metric, r.computed_at desc;

create or replace view serving.zone_card as
with zones as (
    select e.entity_id, e.name, e.attributes ->> 'short' as short_name, e.country_code, e.eic,
           s.value, s.period_start, s.period_end, s.breakdown, s.weights_set_id, s.run_id, s.computed_at
    from ref.entity e
    join feat.latest_score s on s.entity_id = e.entity_id
    where e.kind = 'zone' and e.sensitivity = 'public'
),
flags as (
    -- Datasets of the sources the zone's features read (per measure, the country's precedence) whose last
    -- successful run is older than their SLA.
    select z.entity_id,
           array(select distinct f.source || '.' || f.dataset
                 from core.preferred_series ps
                 join serving.source_freshness f on f.source = ps.source and f.dataset = ps.dataset
                 where ps.entity_id = z.entity_id and f.stale
                 order by 1) as stale_datasets
    from zones z
),
card as (
    select z.*,
           o.best_fit_asset_types, o.asset_types, o.run_id as opportunity_run_id,
           coalesce(o.proposed_assumptions, false) as opportunity_proposed,
           coalesce((z.breakdown ->> 'proposed_assumptions')::boolean, false) as score_proposed,
           (z.breakdown ->> 'unverified_values')::integer as score_unverified,
           rr.low, rr.high, rr.unit, rr.labels, rr.method, rr.price_window_start, rr.price_window_end,
           rr.days_valued, rr.skipped_days, rr.complete_365_day_windows, rr.run_id as revenue_run_id,
           rr.valuation_run_id, rr.computed_at as revenue_computed_at,
           coalesce(rr.proposed_assumptions, false) as revenue_proposed,
           rr.unverified_values as revenue_unverified,
           fl.stale_datasets
    from zones z
    join flags fl on fl.entity_id = z.entity_id
    left join feat.opportunity_summary o on o.entity_id = z.entity_id
    left join serving.return_ranges rr
           on rr.entity_id = z.entity_id and rr.asset_type = 'bess' and rr.metric = 'revenue_eur_per_mw_year'
)
select c.entity_id,
       c.name,
       c.short_name,
       c.country_code,
       c.eic,
       c.value                                                        as congestion_score,
       c.period_start,
       c.period_end,
       coalesce(array(select jsonb_array_elements_text(c.breakdown -> 'labels')), '{}') as score_labels,
       coalesce(array(select jsonb_array_elements_text(c.breakdown -> 'missing_features')), '{}')
                                                                      as missing_features,
       c.weights_set_id,
       c.run_id                                                       as score_run_id,
       c.computed_at                                                  as score_computed_at,
       c.breakdown,
       coalesce(c.best_fit_asset_types, '{}')                         as best_fit_asset_types,
       c.asset_types,
       c.opportunity_run_id,
       c.low                                                          as revenue_low_eur_per_mw_year,
       c.high                                                         as revenue_high_eur_per_mw_year,
       c.unit                                                         as revenue_unit,
       c.labels                                                       as revenue_labels,
       array_to_string(c.labels, ', ')                                as revenue_label_text,
       case when c.method = 'bess_v0_daily_lp_day_ahead' then array[
           'perfect foresight (overstates)',
           'no balancing revenue (understates)',
           'no degradation cost (overstates)',
           'no saturation (overstates as more storage enters)'] end    as revenue_known_biases,
       c.method                                                       as revenue_method,
       c.price_window_start                                           as revenue_window_start,
       c.price_window_end                                             as revenue_window_end,
       c.days_valued                                                  as revenue_days_valued,
       c.skipped_days                                                 as revenue_skipped_days,
       c.complete_365_day_windows                                     as revenue_complete_windows,
       c.revenue_run_id,
       c.valuation_run_id,
       c.revenue_computed_at,
       c.score_proposed                                               as score_proposed_assumptions,
       c.opportunity_proposed                                         as opportunity_proposed_assumptions,
       c.revenue_proposed                                             as revenue_proposed_assumptions,
       c.score_proposed or c.opportunity_proposed or c.revenue_proposed as proposed_assumptions,
       c.score_unverified > 0 or c.revenue_unverified > 0             as unverified_data,
       c.score_unverified                                             as score_unverified_values,
       c.revenue_unverified                                           as revenue_unverified_values,
       cardinality(c.stale_datasets) > 0                              as stale_source,
       c.stale_datasets,
       c.period_end                                                   as data_through,
       array_remove(array[
           case when c.score_proposed or c.opportunity_proposed or c.revenue_proposed
                then 'proposed assumptions (not approved)' end,
           case when c.score_unverified > 0 or c.revenue_unverified > 0 then 'unverified data' end,
           case when cardinality(c.stale_datasets) > 0 then 'stale source' end], null) as labels
from card c;

create or replace view serving.ranked_sites as
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
