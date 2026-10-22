-- migrate:up
-- Raw-data views for holders of the shared API token (docs/ui-contract.md, "Raw data"): role `authenticated`
-- only. PostgREST maps a request with a valid JWT whose role claim is `authenticated` to that role; requests
-- without a token stay `anon` and cannot see these views. Like every serving view they return public rows
-- only (series, entity, codebook entry and the row itself must be `public`); the owner of a view bypasses
-- RLS, so the filter is written out here and tests/test_serving.py checks it for every view.
--
-- Columns are plain and stable: add columns later, never rename or remove.

create view serving.series as
select s.series_key,
       s.entity_id,
       e.name         as entity_name,
       e.kind         as entity_kind,
       e.country_code,
       s.entry_id,
       c.source,
       c.dataset,
       c.field,
       s.unit,
       s.resolution,
       s.kind,
       s.licence
from core.series s
join ref.entity e on e.entity_id = s.entity_id
join ref.codebook_entry c on c.entry_id = s.entry_id
where s.sensitivity = 'public' and e.sensitivity = 'public' and c.sensitivity = 'public';

-- Same rows as core.observation_effective (latest ingest per series, interval_start and issued_at; status as
-- core.review_status maps it), usable ones only: quarantined rows stay in, labelled `unverified` (or `verified`
-- once accepted); rows of a run a person rejected are left out. Written out instead of selecting from the
-- effective view for two reasons. The planner cannot push series_id and interval_start into that view's DISTINCT
-- ON, while here each series is read through observation_lookup_idx (series_id, interval_start, ingested_at) for
-- the requested range only. And the helper functions run as the caller, who has no access to schemas core and
-- ops, so the mapping (core.review_status) and the decision lookup (ops.quarantine_decision_asof('infinity')) are
-- repeated inline; keep them in step with migration 20261018080000_quarantine_review.sql.
create view serving.observations as
select s.series_key,
       l.interval_start,
       l.interval_end,
       l.value,
       l.issued_at,
       l.ingested_at,
       case when l.quality = 'ok' then 'ok'
            when d.decision = 'accept' then 'verified'
            else 'unverified' end as review_status,
       l.run_id
from core.series s
join ref.entity e on e.entity_id = s.entity_id
join ref.codebook_entry c on c.entry_id = s.entry_id
cross join lateral (
    select distinct on (o.interval_start, o.issued_at)
           o.interval_start, o.interval_end, o.value, o.issued_at, o.ingested_at, o.quality, o.run_id
    from core.observation o
    where o.series_id = s.series_id
    order by o.interval_start, o.issued_at, o.ingested_at desc
) l
left join (
    select distinct on (q.run_id) q.run_id, q.decision
    from ops.quarantine_decision q
    order by q.run_id, q.decided_at desc, q.decision_id desc
) d on l.quality = 'quarantined' and d.run_id = l.run_id
where s.sensitivity = 'public' and e.sensitivity = 'public' and c.sensitivity = 'public'
  and (l.quality = 'ok' or d.decision is distinct from 'reject');

create view serving.entities as
select e.entity_id,
       e.kind,
       e.country_code,
       e.parent_id,
       e.name,
       e.eic
from ref.entity e
where e.sensitivity = 'public';

-- Everything the capacity-map loader and other fact writers stored, with history (known_at).
create view serving.facts as
select f.fact_id,
       f.entity_id,
       f.attribute,
       f.value_num,
       f.value_text,
       f.value_json,
       f.unit,
       f.valid_from,
       f.valid_to,
       f.known_at,
       d.source,
       d.url          as source_url,
       f.run_id
from core.fact f
join ref.entity e on e.entity_id = f.entity_id
left join core.source_document d on d.document_id = f.source_document_id and d.sensitivity = 'public'
where f.sensitivity = 'public' and e.sensitivity = 'public';

-- Every computed value of every run (not only the latest); pick a run through run_id and serving.latest_runs.
create view serving.feature_values as
select v.feature_key,
       v.entity_id,
       v.period_start,
       v.period_end,
       v.statistic,
       v.value,
       v.value_text,
       v.run_id,
       v.method_id,
       v.method_version,
       v.computed_at
from feat.feature_value v
join ref.entity e on e.entity_id = v.entity_id
where v.sensitivity = 'public' and e.sensitivity = 'public';

-- Congestion scores of every scenario's latest score run (the card views show the official scenario only).
create view serving.scores_all as
select s.scenario_id,
       s.entity_id,
       s.score_kind,
       s.value        as score,
       s.breakdown,
       s.period_start,
       s.period_end,
       s.weights_set_id,
       s.run_id,
       s.computed_at
from feat.score s
join feat.scenario_run r on r.run_id = s.run_id and r.kind = 'score' and r.name = 'congestion'
join ref.entity e on e.entity_id = s.entity_id
where s.score_kind = 'congestion' and s.sensitivity = 'public' and e.sensitivity = 'public';

-- Default privileges give new serving views to anon too; the raw views are for the token only.
revoke all on serving.series, serving.observations, serving.entities, serving.facts, serving.feature_values,
    serving.scores_all from anon, public;
grant select on serving.series, serving.observations, serving.entities, serving.facts, serving.feature_values,
    serving.scores_all to authenticated;

-- migrate:down
drop view if exists serving.scores_all;
drop view if exists serving.feature_values;
drop view if exists serving.facts;
drop view if exists serving.entities;
drop view if exists serving.observations;
drop view if exists serving.series;
