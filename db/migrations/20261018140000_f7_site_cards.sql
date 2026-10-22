-- migrate:up
-- F7 version 2 on the site cards and the substation map (docs/methods/f7_substation_saturation.md,
-- docs/ui-contract.md). Columns are only appended; nothing is renamed or removed.
--
-- * feat.capacity_map_caveat: the capacity map's own caveat, verbatim from the StoryMap
--   (https://storymaps.arcgis.com/stories/eb5b387e376f49b8996d5e7c47fbdd37, quoted in
--   docs/sources/dk/dso-capacity-maps.md, "Caveat to display on site cards"), and the dossier's English card
--   text, marked as our translation.
-- * serving.ranked_sites gains capacity_map_caveat, capacity_map_caveat_en, no_station_data, low_station_count
--   (from the score's flags) and f7_sample_stations (the station count behind the site's F7 statistic).
-- * serving.substation_map gains consumption_not_assessed (transmission stations: F7 treats the published TSO
--   consumption figures as not assessed, so free_mw_consumption is null and the status unknown; the facts in
--   `capacity` stay as published), capacity_map_caveat and capacity_map_caveat_en.
-- * feat.feature_catalog gains display rows for the F7 statistics the scenarios can score with.

create view feat.capacity_map_caveat as
select
    'Kapacitetskortet må på ingen måde tolkes som en forpligtigelse eller et tilsagn fra hverken netselskaberne '
    'eller Energinet. Før enhver tilslutning skal netselskabet og/eller Energinet udføre beregninger for den '
    'enkelte tilslutning. Kapacitetskortet viser et øjebliksbillede. Allerede dagen efter en opdatering kan der '
    'være indgået aftaler om nye tilslutninger, som kan betyde, at den ledige kapacitet i det pågældende område '
    'ikke længere er tilgængelig. Beregningerne er indikative og kan ikke blive adderet på tværs af stationer.'
        ::text as caveat,
    'Indicative estimate from the national capacity map (Energinet and Green Power Denmark). Not an obligation '
    'or a commitment from the grid companies or Energinet; the exact capacity is only determined after an '
    'agreement with the grid company or Energinet. Station values cannot be added up. (Our translation.)'
        ::text as caveat_en;

create or replace view feat.feature_catalog as
select *
from (values
    ('f1_price_spread', 'f1_price_spread', null, 'Price spread', 'Price spread', null, null,
     'Day-ahead price spread within the day and between zones (F1).', 1),
    ('f1_price_spread', 'f1_intraday_spread_hourly', 'mean', 'Price spread',
     'Intraday price spread (hourly prices)', 'EUR/MWh', null,
     'Mean over local days of the highest minus the lowest day-ahead price, prices averaged to hourly so the '
     'value is comparable across the 2025-10-01 switch to 15-minute prices.', 1),
    ('f1_price_spread', 'f1_intraday_spread_native', 'mean', 'Price spread',
     'Intraday price spread (native resolution)', 'EUR/MWh', null,
     'Mean over local days of the highest minus the lowest day-ahead price at the published resolution.', 1),
    ('f2_curtailment', 'f2_curtailment', null, 'Curtailment proxy', 'Curtailment proxy', null, 'proxy',
     'Curtailment pressure from prices at or below zero and forecast minus actual renewables (F2).', 2),
    ('f2_curtailment', 'f2_nonpositive_price_share', 'share', 'Curtailment proxy',
     'Share of time with a day-ahead price at or below zero', 'share', 'proxy',
     'Duration-weighted share of day-ahead price intervals at or below 0 EUR/MWh; stands in for curtailment.', 2),
    ('f2_curtailment', 'f2_curtailment_proxy_mwh', 'sum', 'Curtailment proxy',
     'Curtailment proxy (forecast minus actual wind and solar)', 'MWh', 'proxy',
     'Over hours with a price at or below zero, the sum of max(0, day-ahead forecast minus actual) for wind '
     'and solar. Forecast error has other causes.', 2),
    ('f3_interconnector_saturation', 'f3_interconnector_saturation', null, 'Interconnector saturation',
     'Interconnector saturation', null, null, 'Not computed yet (Phase 3).', 3),
    ('f4_surplus', 'f4_surplus', null, 'Renewable surplus index', 'Renewable surplus index', null, null,
     'Wind and solar production relative to consumption (F4).', 4),
    ('f4_surplus', 'f4_surplus_index', 'mean', 'Renewable surplus index',
     'Renewable surplus index', 'ratio', null,
     'Mean over intervals of (wind + solar - consumption) / consumption.', 4),
    ('f5_balancing_activation', 'f5_balancing_activation', null, 'Balancing activation',
     'Balancing activation', null, null, 'Balancing direction and imbalance prices (F5).', 5),
    ('f5_balancing_activation', 'f5_imbalance_spot_spread', 'mean', 'Balancing activation',
     'Imbalance to spot price spread', 'EUR/MWh', null,
     'Mean absolute difference between the imbalance price and the day-ahead (spot) price.', 5),
    ('f6_renewable_pipeline', 'f6_renewable_pipeline', null, 'Renewable pipeline', 'Renewable pipeline',
     null, null, 'Not computed yet (Phase 3).', 6),
    ('f7_substation_saturation', 'f7_substation_saturation', null, 'Substation saturation',
     'Substation saturation', null, 'current snapshot',
     'Free capacity at the substations as the capacity map publishes it now (F7).', 7),
    ('f7_substation_saturation', 'f7_substation_saturation', 'share_stations_no_free_production',
     'Substation saturation', 'Share of DSO stations without free production capacity', 'share',
     'current snapshot',
     'Distribution stations whose published free production capacity is 0 MW (below the publisher''s first '
     '20 MW step), among the area''s stations with a published value.', 7),
    ('f7_substation_saturation', 'f7_substation_saturation', 'share_stations_no_free_consumption',
     'Substation saturation', 'Share of DSO stations without free consumption capacity', 'share',
     'current snapshot',
     'Distribution stations whose published free consumption capacity is 0 MW (below the publisher''s first '
     '10 MW step), among the area''s stations with a published value.', 7),
    ('f7_substation_saturation', 'f7_substation_saturation', 'share_stations_free_consumption_no_free_production',
     'Substation saturation', 'Share of DSO stations with room for load where production is full', 'share',
     'current snapshot',
     'Distribution stations with free consumption capacity and 0 MW free production capacity, among the '
     'area''s stations with published values in both directions (for load-type assets).', 7),
    ('f7_substation_saturation', 'f7_substation_saturation', 'share_stations_free_production_no_free_consumption',
     'Substation saturation', 'Share of DSO stations with room for production where consumption is full',
     'share', 'current snapshot',
     'Distribution stations with free production capacity and 0 MW free consumption capacity, among the '
     'area''s stations with published values in both directions (for production-type assets).', 7),
    ('f7_substation_saturation', 'f7_substation_saturation', 'free_mw_consumption_mean_no_free_production',
     'Substation saturation', 'Free consumption MW at production-saturated DSO stations, per station', 'MW',
     'current snapshot',
     'Free consumption MW summed over distribution stations with 0 MW free production capacity, divided by '
     'the area''s stations with published values in both directions. Indicative: station values cannot be '
     'added up.', 7),
    ('f7_substation_saturation', 'f7_substation_saturation', 'free_mw_production_mean_no_free_consumption',
     'Substation saturation', 'Free production MW at consumption-saturated DSO stations, per station', 'MW',
     'current snapshot',
     'Free production MW summed over distribution stations with 0 MW free consumption capacity, divided by '
     'the area''s stations with published values in both directions. Indicative: station values cannot be '
     'added up.', 7),
    ('f7_substation_saturation', 'f7_substation_saturation', 'free_mw_production_mean',
     'Substation saturation', 'Mean free production MW of DSO stations', 'MW', 'current snapshot',
     'Free production MW summed over the area''s distribution stations with a published value, divided by '
     'their number (the map publishes no station capacity). Indicative: station values cannot be added up.', 7),
    ('f7_substation_saturation', 'f7_substation_saturation', 'free_mw_consumption_mean',
     'Substation saturation', 'Mean free consumption MW of DSO stations', 'MW', 'current snapshot',
     'Free consumption MW summed over the area''s distribution stations with a published value, divided by '
     'their number (the map publishes no station capacity). Indicative: station values cannot be added up.', 7)
) as c (weight_key, feature_key, statistic, group_title, title, unit, caveat, description, display_order);

create or replace view serving.substation_map as
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
       sf.capacity_run_ids,
       -- Transmission stations (no DSO attribute): their published consumption figures are not assessed
       -- (scanner.features.substation.NOT_ASSESSED), whatever `capacity` holds.
       not (e.attributes ? 'dso')                     as consumption_not_assessed,
       cv.caveat                                      as capacity_map_caveat,
       cv.caveat_en                                   as capacity_map_caveat_en
from ref.entity e
join ref.entity_geometry g on g.entity_id = e.entity_id and g.sensitivity = 'public'
left join station_facts sf on sf.entity_id = e.entity_id
left join f7 on f7.entity_id = e.entity_id
cross join feat.capacity_map_caveat cv
where e.kind in ('substation', 'transformer')
  and e.sensitivity = 'public'
  and coalesce((e.attributes ->> 'listed')::boolean, true);

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
       coalesce(array(select jsonb_array_elements_text(s.breakdown -> 'flags')), '{}') as score_flags,
       cv.caveat                                                             as capacity_map_caveat,
       cv.caveat_en                                                          as capacity_map_caveat_en,
       coalesce(s.breakdown -> 'flags' ? 'no_station_data', false)           as no_station_data,
       coalesce(s.breakdown -> 'flags' ? 'low_station_count', false)         as low_station_count,
       (s.breakdown -> 'station_sample' -> 'f7_substation_saturation' ->> 'stations')::double precision
                                                                             as f7_sample_stations
from sites s
left join ref.entity z on z.entity_id = s.zone_id
left join components c on c.entity_id = s.entity_id
left join f7 on f7.entity_id = s.entity_id
left join feat.opportunity_summary o on o.entity_id = s.entity_id
left join serving.zone_card zc on zc.entity_id = s.zone_id
cross join feat.capacity_map_caveat cv
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
-- ranked_sites as in 20261018120200, substation_map as in 20261018090100, feature_catalog as in
-- 20261018090000.
drop view if exists serving.ranked_sites;
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

create view serving.ranked_sites as
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

create or replace view feat.feature_catalog as
select *
from (values
    ('f1_price_spread', 'f1_price_spread', null, 'Price spread', 'Price spread', null, null,
     'Day-ahead price spread within the day and between zones (F1).', 1),
    ('f1_price_spread', 'f1_intraday_spread_hourly', 'mean', 'Price spread',
     'Intraday price spread (hourly prices)', 'EUR/MWh', null,
     'Mean over local days of the highest minus the lowest day-ahead price, prices averaged to hourly so the '
     'value is comparable across the 2025-10-01 switch to 15-minute prices.', 1),
    ('f1_price_spread', 'f1_intraday_spread_native', 'mean', 'Price spread',
     'Intraday price spread (native resolution)', 'EUR/MWh', null,
     'Mean over local days of the highest minus the lowest day-ahead price at the published resolution.', 1),
    ('f2_curtailment', 'f2_curtailment', null, 'Curtailment proxy', 'Curtailment proxy', null, 'proxy',
     'Curtailment pressure from prices at or below zero and forecast minus actual renewables (F2).', 2),
    ('f2_curtailment', 'f2_nonpositive_price_share', 'share', 'Curtailment proxy',
     'Share of time with a day-ahead price at or below zero', 'share', 'proxy',
     'Duration-weighted share of day-ahead price intervals at or below 0 EUR/MWh; stands in for curtailment.', 2),
    ('f2_curtailment', 'f2_curtailment_proxy_mwh', 'sum', 'Curtailment proxy',
     'Curtailment proxy (forecast minus actual wind and solar)', 'MWh', 'proxy',
     'Over hours with a price at or below zero, the sum of max(0, day-ahead forecast minus actual) for wind '
     'and solar. Forecast error has other causes.', 2),
    ('f3_interconnector_saturation', 'f3_interconnector_saturation', null, 'Interconnector saturation',
     'Interconnector saturation', null, null, 'Not computed yet (Phase 3).', 3),
    ('f4_surplus', 'f4_surplus', null, 'Renewable surplus index', 'Renewable surplus index', null, null,
     'Wind and solar production relative to consumption (F4).', 4),
    ('f4_surplus', 'f4_surplus_index', 'mean', 'Renewable surplus index',
     'Renewable surplus index', 'ratio', null,
     'Mean over intervals of (wind + solar - consumption) / consumption.', 4),
    ('f5_balancing_activation', 'f5_balancing_activation', null, 'Balancing activation',
     'Balancing activation', null, null, 'Balancing direction and imbalance prices (F5).', 5),
    ('f5_balancing_activation', 'f5_imbalance_spot_spread', 'mean', 'Balancing activation',
     'Imbalance to spot price spread', 'EUR/MWh', null,
     'Mean absolute difference between the imbalance price and the day-ahead (spot) price.', 5),
    ('f6_renewable_pipeline', 'f6_renewable_pipeline', null, 'Renewable pipeline', 'Renewable pipeline',
     null, null, 'Not computed yet (Phase 3).', 6),
    ('f7_substation_saturation', 'f7_substation_saturation', null, 'Substation saturation',
     'Substation saturation', null, 'current snapshot',
     'Free capacity at the substations as the capacity map publishes it now (F7).', 7),
    ('f7_substation_saturation', 'f7_substation_saturation', 'share_stations_no_free_production',
     'Substation saturation', 'Share of DSO stations without free production capacity', 'share',
     'current snapshot',
     'Distribution stations whose published free production capacity is 0 MW (below the publisher''s first '
     '20 MW step), among the area''s stations with a published value.', 7)
) as c (weight_key, feature_key, statistic, group_title, title, unit, caveat, description, display_order);

drop view if exists feat.capacity_map_caveat;

grant select on all tables in schema serving to anon, authenticated, scanner_analyst;
