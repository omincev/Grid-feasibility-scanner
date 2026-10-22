-- migrate:up
-- Views for the front end and API. Only public rows. Columns are a contract with the Lovable UI:
-- add columns freely, never rename or remove without a migration note in the PR.

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

create view serving.opportunities as
select distinct on (o.entity_id, o.asset_type)
       o.entity_id, o.asset_type, o.matched, o.status, o.reasons, o.run_id, o.computed_at
from feat.opportunity o
where o.sensitivity = 'public'
order by o.entity_id, o.asset_type, o.computed_at desc;

create view serving.return_ranges as
select distinct on (r.entity_id, r.asset_type, res.metric)
       r.entity_id, r.asset_type, res.metric, res.low, res.mid, res.high, res.unit,
       r.labels, r.method, r.method_version, r.template_set_id, r.run_id, r.computed_at
from val.run r
join val.result res on res.valuation_run_id = r.valuation_run_id
where r.sensitivity = 'public' and res.sensitivity = 'public'
order by r.entity_id, r.asset_type, res.metric, r.computed_at desc;

create view serving.source_freshness as
select distinct on (source, dataset)
       source, dataset, status, started_at, finished_at, rows_expected, rows_received, rows_written,
       window_start, window_end
from ops.ingest_run
order by source, dataset, started_at desc;

-- migrate:down
drop view if exists serving.source_freshness;
drop view if exists serving.return_ranges;
drop view if exists serving.opportunities;
drop view if exists serving.zone_feature_breakdown;
drop view if exists serving.zone_card;
