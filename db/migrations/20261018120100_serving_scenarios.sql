-- migrate:up
-- Serving by scenario (assumptions/scenarios/README.md, docs/ui-contract.md), non-spatial part; zone_map and
-- ranked_sites follow in 20261018120200_serving_scenarios_spatial.
--
-- One consistent run per scenario: for each scenario, kind and name, the latest finished (ok or partial) run
-- (feat.scenario_run). The existing views show the official scenario's runs only, so feat.latest_score no
-- longer mixes runs per entity (review finding). Rows written before scenarios existed (scenario_id null) are
-- not served.
--
-- Contract notes (columns kept, new ones appended):
-- * scenario_id: zone_card, zone_feature_breakdown, opportunities, return_ranges, latest_runs (one row per kind,
--   name and scenario; null for feature runs and runs written before scenarios).
-- * zone_card.score_flags, zone_feature_breakdown.flag: missing-input flags such as no_station_data.
-- * zone_card.revenue_known_biases now comes from the method card (ref.method_card.known_biases).
-- * New: serving.scenario_catalog, serving.assumption_register, serving.method_catalog,
--   serving.scenario_comparison.

create view feat.official_scenario as
select s.scenario_id from ref.scenario s where s.status = 'official' and s.sensitivity = 'public';

create view feat.scenario_run as
select distinct on (r.scenario_id, r.kind, r.name)
       r.scenario_id, r.kind, r.name, r.run_id, r.status, r.started_at, r.finished_at
from ops.compute_run r
where r.scenario_id is not null and r.status in ('ok', 'partial')
order by r.scenario_id, r.kind, r.name, r.started_at desc;

-- Public congestion scores of each scenario's latest congestion run.
create view feat.scenario_score as
select s.scenario_id, s.entity_id, s.value, s.period_start, s.period_end, s.breakdown, s.weights_set_id,
       s.run_id, s.computed_at, s.method_id, s.method_version
from feat.score s
join feat.scenario_run r on r.run_id = s.run_id and r.kind = 'score' and r.name = 'congestion'
where s.score_kind = 'congestion' and s.sensitivity = 'public';

create or replace view feat.latest_score as
select s.entity_id, s.value, s.period_start, s.period_end, s.breakdown, s.weights_set_id, s.run_id, s.computed_at,
       s.scenario_id
from feat.scenario_score s
join feat.official_scenario o on o.scenario_id = s.scenario_id;

-- One row per scenario, entity and score component, with display metadata. Components of missing inputs
-- (policy null_component) have null values and a flag.
create view feat.scenario_score_component as
select s.scenario_id,
       s.entity_id,
       f.key                                              as feature_key,
       f.value ->> 'weight_key'                           as weight_key,
       f.value ->> 'statistic'                            as statistic,
       coalesce(c.title, g.title, f.key)                  as title,
       coalesce(g.group_title, f.value ->> 'weight_key')  as group_title,
       c.unit,
       coalesce(c.caveat, g.caveat)                       as caveat,
       coalesce(c.description, g.description)            as description,
       (f.value ->> 'raw')::double precision              as raw_value,
       (f.value ->> 'percentile')::double precision       as percentile,
       (f.value ->> 'weight')::double precision           as weight,
       (f.value ->> 'set_weight')::double precision       as set_weight,
       (f.value ->> 'contribution')::double precision     as contribution,
       f.value ->> 'inherited_from'                       as inherited_from,
       (f.value -> 'period' ->> 0)::timestamptz           as period_start,
       (f.value -> 'period' ->> 1)::timestamptz           as period_end,
       f.value -> 'population' ->> 'method'               as population_method,
       (f.value -> 'population' ->> 'n')::integer         as population_n,
       (f.value ->> 'run_id')::uuid                       as feature_run_id,
       (s.breakdown -> 'unverified_inputs' ->> (f.value ->> 'run_id'))::integer as unverified_values,
       coalesce(g.display_order, c.display_order, 99)     as display_order,
       s.run_id,
       f.value ->> 'flag'                                 as flag
from feat.scenario_score s
cross join lateral jsonb_each(s.breakdown -> 'features') f
left join feat.feature_catalog c on c.feature_key = f.key and c.statistic = f.value ->> 'statistic'
left join feat.feature_catalog g on g.weight_key = f.value ->> 'weight_key' and g.statistic is null;

create or replace view feat.score_component as
select sc.entity_id, sc.feature_key, sc.weight_key, sc.statistic, sc.title, sc.group_title, sc.unit, sc.caveat,
       sc.description, sc.raw_value, sc.percentile, sc.weight, sc.set_weight, sc.contribution, sc.inherited_from,
       sc.period_start, sc.period_end, sc.population_method, sc.population_n, sc.feature_run_id,
       sc.unverified_values, sc.display_order, sc.run_id, sc.scenario_id, sc.flag
from feat.scenario_score_component sc
join feat.official_scenario o on o.scenario_id = sc.scenario_id;

-- Public opportunity rows of each scenario's latest opportunity-type run, for public entities.
create view feat.scenario_opportunity as
select o.scenario_id, o.entity_id, o.asset_type, o.matched, o.status, o.reasons, o.run_id, o.computed_at
from feat.opportunity o
join feat.scenario_run r on r.run_id = o.run_id and r.kind = 'score' and r.name = 'opportunity_types'
join ref.entity e on e.entity_id = o.entity_id
where o.sensitivity = 'public' and e.sensitivity = 'public';

-- Public results of each scenario's latest run of every valuation, for public entities.
create view feat.scenario_return_range as
select r.scenario_id, r.entity_id, r.asset_type, res.metric, res.low, res.mid, res.high, res.unit,
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
join feat.scenario_run sr on sr.run_id = r.run_id and sr.kind = 'valuation'
join val.result res on res.valuation_run_id = r.valuation_run_id
join ref.entity e on e.entity_id = r.entity_id
where r.sensitivity = 'public' and res.sensitivity = 'public' and e.sensitivity = 'public';

create or replace view serving.return_ranges as
select rr.entity_id, rr.asset_type, rr.metric, rr.low, rr.mid, rr.high, rr.unit,
       rr.labels, rr.method, rr.method_version, rr.template_set_id, rr.run_id, rr.computed_at,
       rr.valuation_run_id, rr.price_window_start, rr.price_window_end, rr.proposed_assumptions,
       rr.days_valued, rr.skipped_days, rr.complete_365_day_windows, rr.unverified_values,
       rr.scenario_id
from feat.scenario_return_range rr
join feat.official_scenario o on o.scenario_id = rr.scenario_id;

create or replace view serving.opportunities as
select so.entity_id, so.asset_type, so.matched, so.status, so.reasons, so.run_id, so.computed_at, so.scenario_id
from feat.scenario_opportunity so
join feat.official_scenario o on o.scenario_id = so.scenario_id;

-- Opportunity types per entity (official scenario), the matches ordered by the return range midpoint where
-- one exists (docs/features.md). Municipalities use their parent zone's range.
create or replace view feat.opportunity_summary as
with ranked as (
    select l.*,
           (rr.low + rr.high) / 2 as midpoint,
           case l.status when 'match' then 1 when 'candidate_needs_site_data' then 2 else 3 end as status_order
    from serving.opportunities l
    join ref.entity e on e.entity_id = l.entity_id
    left join serving.return_ranges rr
           on rr.entity_id = case when e.kind = 'zone' then e.entity_id else e.parent_id end
          and rr.asset_type = l.asset_type and rr.metric = 'revenue_eur_per_mw_year'
)
select r.entity_id,
       coalesce(array_agg(r.asset_type order by r.midpoint desc nulls last, r.asset_type)
                    filter (where r.status = 'match'), '{}') as best_fit_asset_types,
       jsonb_agg(jsonb_build_object(
           'asset_type', r.asset_type,
           'title', case r.asset_type when 'bess' then 'Battery storage (BESS)'
                                      when 'dsf' then 'Demand-side flexibility (DSF)'
                                      when 'modular_dc' then 'Modular data centre'
                                      else r.asset_type end,
           'status', r.status,
           'status_label', case r.status when 'match' then 'match'
                                         when 'candidate_needs_site_data' then 'candidate, needs site data'
                                         else 'no match' end,
           'matched', r.matched,
           'conditions', r.reasons -> 'conditions')
           order by r.status_order, r.midpoint desc nulls last, r.asset_type) as asset_types,
       bool_or(coalesce((r.reasons ->> 'proposed_assumptions')::boolean, false)) as proposed_assumptions,
       (array_agg(r.run_id order by r.computed_at desc))[1] as run_id,
       r.scenario_id
from ranked r
group by r.entity_id, r.scenario_id;

create or replace view serving.zone_card as
with zones as (
    select e.entity_id, e.name, e.attributes ->> 'short' as short_name, e.country_code, e.eic,
           s.value, s.period_start, s.period_end, s.breakdown, s.weights_set_id, s.run_id, s.computed_at,
           s.scenario_id
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
       (select mc.known_biases from ref.method_card mc
        where mc.method_id = c.method and mc.sensitivity = 'public') as revenue_known_biases,
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
           case when cardinality(c.stale_datasets) > 0 then 'stale source' end], null) as labels,
       c.scenario_id,
       coalesce(array(select jsonb_array_elements_text(c.breakdown -> 'flags')), '{}') as score_flags
from card c;

create or replace view serving.zone_feature_breakdown as
select sc.entity_id,
       sc.feature_key,
       sc.raw_value,
       sc.percentile,
       sc.weight,
       sc.contribution,
       sc.run_id,
       sc.weight_key,
       sc.statistic,
       sc.title,
       sc.group_title,
       sc.unit,
       sc.caveat,
       sc.description,
       sc.set_weight,
       sc.period_start,
       sc.period_end,
       sc.population_method,
       sc.population_n,
       sc.feature_run_id,
       sc.unverified_values,
       sc.display_order,
       sc.scenario_id,
       sc.flag
from feat.score_component sc
join ref.entity e on e.entity_id = sc.entity_id
where e.kind = 'zone' and e.sensitivity = 'public';

-- Latest finished (ok or partial) derived-data run per kind, name and scenario, and the latest attempt.
create or replace view serving.latest_runs as
select k.kind, k.name,
       ok.run_id, ok.status, ok.started_at, ok.finished_at, ok.code_version,
       att.status as last_attempt_status, att.started_at as last_attempt_at,
       k.scenario_id
from (select distinct kind, name, scenario_id from ops.compute_run) k
left join lateral (
    select r.run_id, r.status, r.started_at, r.finished_at, r.code_version from ops.compute_run r
    where r.kind = k.kind and r.name = k.name and r.scenario_id is not distinct from k.scenario_id
      and r.status in ('ok', 'partial')
    order by r.started_at desc limit 1
) ok on true
left join lateral (
    select r.status, r.started_at from ops.compute_run r
    where r.kind = k.kind and r.name = k.name and r.scenario_id is not distinct from k.scenario_id
    order by r.started_at desc limit 1
) att on true;

-- Every scenario with its hashes and its latest runs.
create view serving.scenario_catalog as
select s.scenario_id,
       s.title,
       s.status,
       s.description,
       s.content_hash,
       coalesce((select jsonb_object_agg(a.set_id, a.content_hash)
                 from ref.assumption_set a
                 where a.sensitivity = 'public'
                   and a.set_id in (select v from jsonb_each_text(s.sets) as x (k, v))), '{}') as assumption_hashes,
       s.sets,
       s.method_switches,
       s.overrides,
       s.owners,
       s.approved_by,
       s.approved_on,
       (select r.run_id from feat.scenario_run r
        where r.scenario_id = s.scenario_id and r.kind = 'score' and r.name = 'congestion') as score_run_id,
       (select r.run_id from feat.scenario_run r
        where r.scenario_id = s.scenario_id and r.kind = 'score' and r.name = 'opportunity_types')
                                                                      as opportunity_run_id,
       (select r.run_id from feat.scenario_run r
        where r.scenario_id = s.scenario_id and r.kind = 'valuation'
        order by r.started_at desc limit 1)                           as valuation_run_id,
       s.source_path,
       s.loaded_at
from ref.scenario s
where s.sensitivity = 'public';

-- Every parameter of every public assumption set, with the scenarios that use the set and the values
-- scenarios set for the parameter (method switches and overrides).
create view serving.assumption_register as
with uses as (
    select s.scenario_id, r.role, r.set_id, s.method_switches || s.overrides as replaced
    from ref.scenario s
    cross join lateral jsonb_each_text(s.sets) as r (role, set_id)
    where s.sensitivity = 'public'
)
select a.set_id,
       a.kind                                                   as set_kind,
       a.status                                                 as set_status,
       p.key                                                    as parameter,
       nullif(p.value -> 'value', 'null'::jsonb)                as value,
       nullif(p.value -> 'proposed', 'null'::jsonb)             as proposed,
       p.value ->> 'note'                                       as note,
       a.owners,
       case when nullif(p.value -> 'value', 'null'::jsonb) is not null then 'value_set'
            when nullif(p.value -> 'proposed', 'null'::jsonb) is not null then 'proposed_only'
            else 'missing' end                                  as status,
       a.approved_by,
       a.approved_on,
       coalesce((select array_agg(distinct u.scenario_id order by u.scenario_id)
                 from uses u where u.set_id = a.set_id), '{}')  as scenarios,
       (select jsonb_object_agg(u.scenario_id, u.replaced -> (u.role || '.' || p.key))
        from uses u
        where u.set_id = a.set_id and u.replaced ? (u.role || '.' || p.key)) as scenario_values,
       coalesce((select array_agg(distinct u.role order by u.role)
                 from uses u where u.set_id = a.set_id), '{}')  as roles,
       a.content_hash,
       a.source_path
from ref.assumption_set a
cross join lateral jsonb_each(a.parameters) p
where a.sensitivity = 'public';

create view serving.method_catalog as
select m.method_id, m.version, m.title, m.status, m.owner, m.inputs, m.parameters, m.outputs, m.known_biases,
       m.body, m.content_hash, m.source_path, m.loaded_at
from ref.method_card m
where m.sensitivity = 'public';

-- Per scored entity and scenario: score, rank and the BESS range (a municipality shows its zone's range),
-- with the differences to the official scenario.
create view serving.scenario_comparison as
with scores as (
    select s.scenario_id, s.entity_id, e.name, e.kind,
           case when e.kind = 'zone' then e.entity_id
                else coalesce(s.breakdown ->> 'parent_zone', e.parent_id) end   as zone_id,
           s.value, s.run_id,
           coalesce(array(select jsonb_array_elements_text(s.breakdown -> 'flags')), '{}') as score_flags,
           coalesce((s.breakdown ->> 'proposed_assumptions')::boolean, false) as proposed
    from feat.scenario_score s
    join ref.entity e on e.entity_id = s.entity_id
    where e.sensitivity = 'public'
),
ranked as (
    select sc.*,
           rank() over (partition by sc.scenario_id, sc.kind order by sc.value desc)              as rank,
           rank() over (partition by sc.scenario_id, sc.kind, sc.zone_id order by sc.value desc)  as rank_in_zone
    from scores sc
),
joined as (
    select r.*, rr.low, rr.high, rr.run_id as revenue_run_id, rr.entity_id as revenue_entity_id
    from ranked r
    left join feat.scenario_return_range rr
           on rr.scenario_id = r.scenario_id and rr.entity_id = r.zone_id
          and rr.asset_type = 'bess' and rr.metric = 'revenue_eur_per_mw_year'
)
select j.entity_id,
       j.name,
       j.kind,
       j.zone_id,
       j.scenario_id,
       sc.title                         as scenario_title,
       sc.status                        as scenario_status,
       j.value                          as congestion_score,
       j.rank,
       j.rank_in_zone,
       j.low                            as revenue_low_eur_per_mw_year,
       j.high                           as revenue_high_eur_per_mw_year,
       o.scenario_id                    as official_scenario_id,
       o.value                          as official_congestion_score,
       o.rank                           as official_rank,
       j.value - o.value                as score_delta,
       j.rank - o.rank                  as rank_delta,
       j.rank_in_zone - o.rank_in_zone  as rank_in_zone_delta,
       j.low - o.low                    as revenue_low_delta,
       j.high - o.high                  as revenue_high_delta,
       j.score_flags,
       j.proposed                       as proposed_assumptions,
       j.run_id                         as score_run_id,
       j.revenue_run_id,
       j.revenue_entity_id,
       exists (select 1 from ref.benchmark b
               where b.entity_id = j.entity_id and b.sensitivity = 'public') as is_benchmark
from joined j
join ref.scenario sc on sc.scenario_id = j.scenario_id and sc.sensitivity = 'public'
left join joined o on o.entity_id = j.entity_id and o.scenario_id = (select scenario_id from feat.official_scenario);

grant usage on schema serving to anon, authenticated, scanner_analyst;
grant select on all tables in schema serving to anon, authenticated, scanner_analyst;

-- migrate:down
-- The views as before scenarios (20261018090000, 20261018090100, 20261018110000).
drop view if exists serving.scenario_comparison;
drop view if exists serving.method_catalog;
drop view if exists serving.assumption_register;
drop view if exists serving.scenario_catalog;
drop view if exists serving.ranked_sites;
drop view if exists serving.zone_map;
drop view if exists serving.zone_feature_breakdown;
drop view if exists serving.zone_card;
drop view if exists feat.opportunity_summary;
drop view if exists serving.opportunities;
drop view if exists serving.return_ranges;
drop view if exists serving.latest_runs;
drop view if exists feat.score_component;
drop view if exists feat.latest_score;
drop view if exists feat.scenario_return_range;
drop view if exists feat.scenario_opportunity;
drop view if exists feat.scenario_score_component;
drop view if exists feat.scenario_score;
drop view if exists feat.scenario_run;
drop view if exists feat.official_scenario;

create view feat.latest_score as
select distinct on (s.entity_id)
       s.entity_id, s.value, s.period_start, s.period_end, s.breakdown, s.weights_set_id, s.run_id, s.computed_at
from feat.score s
where s.score_kind = 'congestion' and s.sensitivity = 'public'
order by s.entity_id, s.computed_at desc, s.run_id;

create view feat.score_component as
select s.entity_id,
       f.key                                              as feature_key,
       f.value ->> 'weight_key'                           as weight_key,
       f.value ->> 'statistic'                            as statistic,
       coalesce(c.title, g.title, f.key)                  as title,
       coalesce(g.group_title, f.value ->> 'weight_key')  as group_title,
       c.unit,
       coalesce(c.caveat, g.caveat)                       as caveat,
       coalesce(c.description, g.description)            as description,
       (f.value ->> 'raw')::double precision              as raw_value,
       (f.value ->> 'percentile')::double precision       as percentile,
       (f.value ->> 'weight')::double precision           as weight,
       (f.value ->> 'set_weight')::double precision       as set_weight,
       (f.value ->> 'contribution')::double precision     as contribution,
       f.value ->> 'inherited_from'                       as inherited_from,
       (f.value -> 'period' ->> 0)::timestamptz           as period_start,
       (f.value -> 'period' ->> 1)::timestamptz           as period_end,
       f.value -> 'population' ->> 'method'               as population_method,
       (f.value -> 'population' ->> 'n')::integer         as population_n,
       (f.value ->> 'run_id')::uuid                       as feature_run_id,
       -- unverified values the feature run loaded (for every zone of the run: percentiles pool them)
       (s.breakdown -> 'unverified_inputs' ->> (f.value ->> 'run_id'))::integer as unverified_values,
       coalesce(g.display_order, c.display_order, 99)     as display_order,
       s.run_id
from feat.latest_score s
cross join lateral jsonb_each(s.breakdown -> 'features') f
left join feat.feature_catalog c on c.feature_key = f.key and c.statistic = f.value ->> 'statistic'
left join feat.feature_catalog g on g.weight_key = f.value ->> 'weight_key' and g.statistic is null;

create view serving.return_ranges as
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

create view serving.opportunities as
select distinct on (o.entity_id, o.asset_type)
       o.entity_id, o.asset_type, o.matched, o.status, o.reasons, o.run_id, o.computed_at
from feat.opportunity o
join ref.entity e on e.entity_id = o.entity_id
where o.sensitivity = 'public' and e.sensitivity = 'public'
order by o.entity_id, o.asset_type, o.computed_at desc;

create view feat.opportunity_summary as
with latest as (
    select distinct on (o.entity_id, o.asset_type)
           o.entity_id, o.asset_type, o.matched, o.status, o.reasons, o.rules_set_id, o.run_id,
           o.computed_at, o.sensitivity
    from feat.opportunity o
    where o.sensitivity = 'public'
    order by o.entity_id, o.asset_type, o.computed_at desc
),
ranked as (
    select l.*,
           (rr.low + rr.high) / 2 as midpoint,
           case l.status when 'match' then 1 when 'candidate_needs_site_data' then 2 else 3 end as status_order
    from latest l
    join ref.entity e on e.entity_id = l.entity_id
    left join serving.return_ranges rr
           on rr.entity_id = case when e.kind = 'zone' then e.entity_id else e.parent_id end
          and rr.asset_type = l.asset_type and rr.metric = 'revenue_eur_per_mw_year'
)
select r.entity_id,
       coalesce(array_agg(r.asset_type order by r.midpoint desc nulls last, r.asset_type)
                    filter (where r.status = 'match'), '{}') as best_fit_asset_types,
       jsonb_agg(jsonb_build_object(
           'asset_type', r.asset_type,
           'title', case r.asset_type when 'bess' then 'Battery storage (BESS)'
                                      when 'dsf' then 'Demand-side flexibility (DSF)'
                                      when 'modular_dc' then 'Modular data centre'
                                      else r.asset_type end,
           'status', r.status,
           'status_label', case r.status when 'match' then 'match'
                                         when 'candidate_needs_site_data' then 'candidate, needs site data'
                                         else 'no match' end,
           'matched', r.matched,
           'conditions', r.reasons -> 'conditions')
           order by r.status_order, r.midpoint desc nulls last, r.asset_type) as asset_types,
       bool_or(coalesce((r.reasons ->> 'proposed_assumptions')::boolean, false)) as proposed_assumptions,
       (array_agg(r.run_id order by r.computed_at desc))[1] as run_id
from ranked r
group by r.entity_id;

create view serving.zone_card as
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

create view serving.zone_feature_breakdown as
select sc.entity_id,
       sc.feature_key,
       sc.raw_value,
       sc.percentile,
       sc.weight,
       sc.contribution,
       sc.run_id,
       sc.weight_key,
       sc.statistic,
       sc.title,
       sc.group_title,
       sc.unit,
       sc.caveat,
       sc.description,
       sc.set_weight,
       sc.period_start,
       sc.period_end,
       sc.population_method,
       sc.population_n,
       sc.feature_run_id,
       sc.unverified_values,
       sc.display_order
from feat.score_component sc
join ref.entity e on e.entity_id = sc.entity_id
where e.kind = 'zone' and e.sensitivity = 'public';

create view serving.latest_runs as
select k.kind, k.name,
       ok.run_id, ok.status, ok.started_at, ok.finished_at, ok.code_version,
       att.status as last_attempt_status, att.started_at as last_attempt_at
from (select distinct kind, name from ops.compute_run) k
left join lateral (
    select r.run_id, r.status, r.started_at, r.finished_at, r.code_version from ops.compute_run r
    where r.kind = k.kind and r.name = k.name and r.status in ('ok', 'partial')
    order by r.started_at desc limit 1
) ok on true
left join lateral (
    select r.status, r.started_at from ops.compute_run r
    where r.kind = k.kind and r.name = k.name
    order by r.started_at desc limit 1
) att on true;

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
