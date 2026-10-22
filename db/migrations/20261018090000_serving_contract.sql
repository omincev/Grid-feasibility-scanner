-- migrate:up
-- Serving contract for the hackathon UI (WP1.10, docs/ui-contract.md), non-spatial part. The spatial views
-- (zone_map, substation_map, ranked_sites) follow in 20261018090100_serving_contract_spatial.
--
-- Unverified data: the counts of unverified (quarantined, undecided) input values that feature, score and
-- valuation runs record (20261018080000_quarantine_review, scanner.runtime.quarantine); null for outputs
-- written before the counts existed.
--
-- Contract notes (the UI is built on these names from 21 October; add columns freely, rename only with a note):
-- * serving.zone_card is rebuilt: run_id -> score_run_id, computed_at -> score_computed_at; new opportunity,
--   revenue, flag and freshness columns. Rows: zones with a public congestion score, as before.
-- * serving.zone_feature_breakdown keeps its columns and adds title, unit, caveat and provenance.
-- * serving.source_freshness is rebuilt per dataset (manifests, snapshots, runtime loaders) with SLA and stale.
-- * serving.return_ranges gains columns at the end; serving.opportunities is unchanged.
-- * New: serving.feature_catalog, serving.latest_runs.
-- Helper views in feat and core are not exposed to anon; serving views read them as their owner.

-- Dataset metadata from the manifests and registry/snapshots.yaml (scanner.core.curated, folder "datasets"),
-- so freshness can be judged against each manifest's SLA.
create table ref.dataset (
    source             text not null,
    dataset            text not null,
    kind               text not null check (kind in ('manifest', 'snapshot')),
    country_code       text,
    status             text not null,
    schedule           text[] not null default '{}',
    schedule_timezone  text,
    freshness_sla      text,          -- ISO 8601 duration as in the manifest, e.g. PT36H
    resolution         text,
    history_start      date,
    history_end        date,
    licence            text,
    owner              text,
    sensitivity        core.sensitivity not null default 'public',
    source_path        text not null,
    loaded_at          timestamptz not null default now(),
    primary key (source, dataset)
);
-- The 0900 policy loop has already run, so policies and grants are explicit here.
alter table ref.dataset enable row level security;
create policy ingest_all on ref.dataset to scanner_ingest, scanner_curator using (true) with check (true);
create policy analyst_read on ref.dataset for select to scanner_analyst using (sensitivity <= 'nda');
grant select on ref.dataset to scanner_ingest, scanner_analyst;
grant select, insert, update, delete on ref.dataset to scanner_curator;

-- Serving lookups of the feature values of one run (feat.f7_latest).
create index feature_value_run_idx on feat.feature_value (run_id, feature_key);

-- Display metadata for score components: group rows (statistic null) per weight key, component rows per
-- (feature_key, statistic) as written by scanner.scores. Caveats as docs/features.md demands them.
create view feat.feature_catalog as
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

-- The series an entity's features read per equivalence class: the only source, or the first one its
-- country file lists in `precedence` (as scanner.features.inputs.select_source); every source when the
-- class has no precedence entry.
create view core.preferred_series as
with s as (
    select s.series_id, s.entity_id, ce.equivalence_class, ce.source, ce.dataset,
           (select p.ord
            from ref.country c
            cross join lateral jsonb_array_elements_text(c.config -> 'precedence' -> ce.equivalence_class)
                with ordinality as p (source, ord)
            where c.code = e.country_code and p.source = ce.source) as rank
    from core.series s
    join ref.codebook_entry ce on ce.entry_id = s.entry_id
    join ref.entity e on e.entity_id = s.entity_id
    where s.sensitivity = 'public'
)
select series_id, entity_id, equivalence_class, source, dataset
from (
    select s.*,
           min(s.source) over w = max(s.source) over w as one_source,
           min(s.rank) over w as best
    from s
    window w as (partition by s.entity_id, s.equivalence_class)
) x
where x.one_source or x.best is null or x.rank = x.best;

-- Latest public congestion score per entity.
create view feat.latest_score as
select distinct on (s.entity_id)
       s.entity_id, s.value, s.period_start, s.period_end, s.breakdown, s.weights_set_id, s.run_id, s.computed_at
from feat.score s
where s.score_kind = 'congestion' and s.sensitivity = 'public'
order by s.entity_id, s.computed_at desc, s.run_id;

-- One row per entity and score component (feat.score.breakdown -> features), with display metadata.
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

-- Rows of the latest ok or partial F7 run (scanner.features.substation statistics).
create view feat.f7_latest as
select fv.entity_id, fv.statistic, fv.value, fv.run_id
from feat.feature_value fv
where fv.feature_key = 'f7_substation_saturation'
  and fv.sensitivity = 'public'
  and fv.run_id = (
      select r.run_id from ops.compute_run r
      where r.kind = 'feature' and r.name = 'f7_substation_saturation' and r.status in ('ok', 'partial')
      order by r.started_at desc
      limit 1
  );

-- serving.return_ranges: same columns, plus the val.run details at the end.
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

-- Opportunity types per entity: latest public row per asset type, the matches ordered by the return range
-- midpoint where one exists (docs/features.md). Municipalities use their parent zone's range.
create view feat.opportunity_summary as
with latest as (
    select distinct on (o.entity_id, o.asset_type) o.*
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

drop view if exists serving.zone_feature_breakdown;
drop view if exists serving.zone_card;
drop view if exists serving.source_freshness;

-- Per dataset: the last successful run and the last attempt, rows expected and received for the newest
-- checked local day, the SLA and stale: the last successful run finished longer ago than the SLA (the
-- last-update rule of Dagster freshness checks); null without an SLA.
create view serving.source_freshness as
with datasets as (
    select d.source, d.dataset, d.kind, d.country_code, d.status, d.schedule, d.schedule_timezone,
           d.freshness_sla, d.resolution, d.licence
    from ref.dataset d
    where d.sensitivity = 'public'
    union all
    -- Runtime loaders without a manifest (the capacity map): runs, but no registered SLA or schedule.
    select distinct r.source, r.dataset, 'loader', null, null, '{}'::text[], null, null, null, null
    from ops.ingest_run r
    where not exists (select 1 from ref.dataset d where d.source = r.source and d.dataset = r.dataset)
),
runs as (
    select d.*,
           ok.finished_at as ok_at, ok.run_id as ok_run_id, ok.partition_key as ok_partition,
           fp.severity as ok_severity,
           att.started_at as attempt_at, att.status as attempt_status, att.run_id as attempt_run_id,
           chk.partition_key as checked_partition, chk.rows_expected, chk.rows_received
    from datasets d
    left join lateral (
        select r.run_id, r.finished_at, r.partition_key from ops.ingest_run r
        where r.source = d.source and r.dataset = d.dataset and r.status = 'ok'
        order by r.started_at desc limit 1
    ) ok on d.kind <> 'snapshot'
    left join ops.fingerprint fp on fp.run_id = ok.run_id
    left join lateral (
        select r.run_id, r.started_at, r.status from ops.ingest_run r
        where r.source = d.source and r.dataset = d.dataset
        order by r.started_at desc limit 1
    ) att on d.kind <> 'snapshot'
    left join lateral (  -- the newest local day with a completeness check (today's partition has none)
        select r.partition_key, r.rows_expected, r.rows_received from ops.ingest_run r
        where r.source = d.source and r.dataset = d.dataset and r.status = 'ok' and r.rows_expected is not null
        order by r.partition_key desc, r.started_at desc limit 1
    ) chk on d.kind <> 'snapshot'
),
snaps as (
    select r.*,
           sok.fetched_at as snap_ok_at,
           sat.fetched_at as snap_attempt_at,
           case when sat.fetched_at is null then null
                when sat.error is null and sat.status_code between 200 and 299 then 'ok' else 'failed' end
               as snap_attempt_status,
           (select max(s.fetched_at) from ops.snapshot s where s.snapshot_id = r.dataset and s.changed)
               as last_changed_at
    from runs r
    left join lateral (
        select s.fetched_at from ops.snapshot s
        where s.snapshot_id = r.dataset and s.error is null and s.status_code between 200 and 299
        order by s.fetched_at desc limit 1
    ) sok on r.kind = 'snapshot'
    left join lateral (
        select s.fetched_at, s.error, s.status_code from ops.snapshot s
        where s.snapshot_id = r.dataset
        order by s.fetched_at desc limit 1
    ) sat on r.kind = 'snapshot'
),
merged as (
    select s.*,
           coalesce(s.ok_at, s.snap_ok_at) as last_success_at,
           s.freshness_sla::interval as sla
    from snaps s
)
select m.source,
       src.name                                                   as source_name,
       m.dataset,
       m.kind,
       m.country_code,
       m.status                                                   as registry_status,
       m.schedule,
       m.schedule_timezone,
       m.resolution,
       m.licence,
       m.freshness_sla,
       (extract(epoch from m.sla) / 3600)::double precision       as freshness_sla_hours,
       m.last_success_at,
       m.ok_run_id                                                as last_success_run_id,
       m.ok_partition                                             as last_success_partition,
       m.ok_severity = 'soft'                                     as last_success_quarantined,
       (extract(epoch from now() - m.last_success_at) / 3600)::double precision as hours_since_success,
       coalesce(m.attempt_at, m.snap_attempt_at)                  as last_attempt_at,
       coalesce(m.attempt_status, m.snap_attempt_status)          as last_attempt_status,
       m.attempt_run_id                                           as last_attempt_run_id,
       m.checked_partition                                        as rows_partition,
       m.rows_expected,
       m.rows_received,
       m.rows_received >= m.rows_expected                         as rows_complete,
       m.last_changed_at,
       (select max(lo.interval_end)
        from core.series s
        join ref.codebook_entry ce on ce.entry_id = s.entry_id
        cross join lateral (
            select o.interval_end from core.observation o
            where o.series_id = s.series_id
            order by o.interval_start desc limit 1
        ) lo
        where ce.source = m.source and ce.dataset = m.dataset and s.sensitivity = 'public'
       )                                                          as data_through,
       case when m.sla is null then null
            else m.last_success_at is null or now() - m.last_success_at > m.sla end as stale
from merged m
left join ref.source src on src.source = m.source;

-- One row per zone with a public congestion score: everything the zone card shows.
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

-- One row per zone and score component; old columns first (entity_id, feature_key, raw_value, percentile,
-- weight, contribution, run_id = the score run).
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

create view serving.feature_catalog as
select weight_key, feature_key, statistic, group_title, title, unit, caveat, description, display_order
from feat.feature_catalog;

-- Latest finished (ok or partial) derived-data run per kind and name, and the latest attempt of any status.
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

grant usage on schema serving to anon, authenticated, scanner_analyst;
grant select on all tables in schema serving to anon, authenticated, scanner_analyst;

-- migrate:down
drop view if exists serving.latest_runs;
drop view if exists serving.feature_catalog;
drop view if exists serving.zone_feature_breakdown;
drop view if exists serving.zone_card;
drop view if exists serving.source_freshness;
drop view if exists feat.opportunity_summary;
drop view if exists feat.f7_latest;
drop view if exists feat.score_component;
drop view if exists feat.latest_score;
drop view if exists core.preferred_series;
drop view if exists feat.feature_catalog;
drop index if exists feat.feature_value_run_idx;
drop table if exists ref.dataset;
-- So that load-curated fills ref.dataset again if this migration is reapplied.
delete from ref.curated_file where folder = 'datasets';

-- serving.return_ranges as in 20261017000800 (columns cannot be dropped with create or replace).
drop view if exists serving.ranked_sites;
drop view if exists serving.return_ranges;
create view serving.return_ranges as
select distinct on (r.entity_id, r.asset_type, res.metric)
       r.entity_id, r.asset_type, res.metric, res.low, res.mid, res.high, res.unit,
       r.labels, r.method, r.method_version, r.template_set_id, r.run_id, r.computed_at
from val.run r
join val.result res on res.valuation_run_id = r.valuation_run_id
where r.sensitivity = 'public' and res.sensitivity = 'public'
order by r.entity_id, r.asset_type, res.metric, r.computed_at desc;

-- serving.ranked_sites as in 20261017000810.
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

-- serving.zone_card, zone_feature_breakdown and source_freshness as in 20261017000800.
create view serving.zone_card as
with latest as (
    select distinct on (s.entity_id) s.*
    from feat.score s
    where s.score_kind = 'congestion' and s.sensitivity = 'public'
    order by s.entity_id, s.computed_at desc
)
select e.entity_id,
       e.name,
       e.country_code,
       e.eic,
       l.value       as congestion_score,
       l.period_start,
       l.period_end,
       l.breakdown,
       l.weights_set_id,
       l.run_id,
       l.computed_at
from ref.entity e
join latest l on l.entity_id = e.entity_id
where e.kind = 'zone' and e.sensitivity = 'public';

create view serving.zone_feature_breakdown as
select c.entity_id,
       f.key                                  as feature_key,
       (f.value ->> 'raw')::double precision  as raw_value,
       (f.value ->> 'percentile')::double precision as percentile,
       (f.value ->> 'weight')::double precision     as weight,
       (f.value ->> 'contribution')::double precision as contribution,
       c.run_id
from serving.zone_card c
cross join lateral jsonb_each(c.breakdown -> 'features') f;

create view serving.source_freshness as
select distinct on (source, dataset)
       source, dataset, status, started_at, finished_at, rows_expected, rows_received, rows_written,
       window_start, window_end
from ops.ingest_run
order by source, dataset, started_at desc;

grant select on all tables in schema serving to anon, authenticated, scanner_analyst;
