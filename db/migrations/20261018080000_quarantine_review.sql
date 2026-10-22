-- migrate:up
-- Review of quarantined ingest runs (ADR 0013, docs/quality.md layer 2; scanner.runtime.quarantine).
-- Rows written under a soft fingerprint deviation keep quality 'quarantined' in core.observation, which is
-- never updated (ADR 0003). A person records decisions per ingest run in ops.quarantine_decision, append-only;
-- the decision in force is the run's latest (decided_at, then decision_id). Readers use
-- core.observation_effective:
--   review_status 'ok'          quality ok
--                 'unverified'  quarantined, no decision     usable, labelled unverified
--                 'verified'    quarantined, then accepted   usable, label cleared
--                 'rejected'    quarantined, then rejected   excluded (usable = false)

alter table ops.quarantine_decision
    add constraint quarantine_decision_named check (btrim(decided_by) <> '' and btrim(reason) <> '');

-- The decision in force per run as known at p_known_at; 'infinity' gives the latest.
create function ops.quarantine_decision_asof(p_known_at timestamptz)
returns table (run_id uuid, decision_id bigint, decision text, decided_by text, reason text, decided_at timestamptz)
language sql stable as $$
    select distinct on (d.run_id) d.run_id, d.decision_id, d.decision, d.decided_by, d.reason, d.decided_at
    from ops.quarantine_decision d
    where d.decided_at <= p_known_at
    order by d.run_id, d.decided_at desc, d.decision_id desc
$$;

-- The one place that maps stored quality and the decision in force to a review status.
create function core.review_status(p_quality core.quality, p_decision text)
returns text
language sql immutable as $$
    select case
        when p_quality = 'ok' then 'ok'
        when p_decision = 'accept' then 'verified'
        when p_decision = 'reject' then 'rejected'
        else 'unverified'
    end
$$;

create view core.observation_effective as
select o.series_id, o.interval_start, o.interval_end, o.issued_at, o.ingested_at, o.value, o.run_id, o.quality,
       core.review_status(o.quality, d.decision) as review_status,
       d.decision_id,
       core.review_status(o.quality, d.decision) <> 'rejected' as usable
from core.observation_latest o
left join ops.quarantine_decision_asof('infinity') d on o.quality = 'quarantined' and d.run_id = o.run_id;

-- What was known at p_known_at (ADR 0003), reviewed with the decisions made by then: a backtest as of t
-- sees a run as unverified until its decision's decided_at, and still uses it unless rejected by t.
create function core.observation_effective_asof(p_known_at timestamptz)
returns table (
    series_id bigint, interval_start timestamptz, interval_end timestamptz, issued_at timestamptz,
    ingested_at timestamptz, value double precision, run_id uuid, quality core.quality,
    review_status text, decision_id bigint, usable boolean
)
language sql stable as $$
    select o.series_id, o.interval_start, o.interval_end, o.issued_at, o.ingested_at, o.value, o.run_id, o.quality,
           core.review_status(o.quality, d.decision),
           d.decision_id,
           core.review_status(o.quality, d.decision) <> 'rejected'
    from core.observation_asof(p_known_at) o
    left join ops.quarantine_decision_asof(p_known_at) d on o.quality = 'quarantined' and d.run_id = o.run_id
$$;

-- The 0900 grants covered only the objects that existed then.
grant select on core.observation_effective to scanner_ingest, scanner_analyst;

-- migrate:down
drop function if exists core.observation_effective_asof(timestamptz);
drop view if exists core.observation_effective;
drop function if exists core.review_status(core.quality, text);
drop function if exists ops.quarantine_decision_asof(timestamptz);
alter table ops.quarantine_decision drop constraint if exists quarantine_decision_named;
