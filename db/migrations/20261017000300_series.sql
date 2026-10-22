-- migrate:up
-- Dense time series (ADR 0001, 0002, 0003).

create table core.series (
    series_id    bigserial primary key,
    series_key   text not null unique,
    entry_id     text not null references ref.codebook_entry (entry_id),
    entity_id    text not null references ref.entity (entity_id),
    unit         text not null,
    resolution   interval not null,
    kind         core.series_kind not null,
    sensitivity  core.sensitivity not null default 'public',
    licence      text,
    created_at   timestamptz not null default now()
);
create index series_entity_idx on core.series (entity_id);
create index series_entry_idx on core.series (entry_id);

-- Sensitivity is inherited from core.series (RLS policy joins on series_id).
create table core.observation (
    series_id       bigint not null references core.series (series_id),
    interval_start  timestamptz not null,
    interval_end    timestamptz not null,
    issued_at       timestamptz,
    ingested_at     timestamptz not null default now(),
    value           double precision,
    run_id          uuid not null,
    quality         core.quality not null default 'ok',
    check (interval_end > interval_start)
);
create unique index observation_key_idx
    on core.observation (series_id, interval_start, coalesce(issued_at, '-infinity'::timestamptz), ingested_at);
create index observation_lookup_idx
    on core.observation (series_id, interval_start, ingested_at desc);
create index observation_start_brin on core.observation using brin (interval_start);

create view core.observation_latest as
select distinct on (series_id, interval_start, issued_at)
       series_id, interval_start, interval_end, issued_at, ingested_at, value, run_id, quality
from core.observation
order by series_id, interval_start, issued_at, ingested_at desc;

-- What was known at p_known_at (ADR 0003).
create function core.observation_asof(p_known_at timestamptz)
returns table (
    series_id bigint, interval_start timestamptz, interval_end timestamptz, issued_at timestamptz,
    ingested_at timestamptz, value double precision, run_id uuid, quality core.quality
)
language sql stable as $$
    select distinct on (o.series_id, o.interval_start, o.issued_at)
           o.series_id, o.interval_start, o.interval_end, o.issued_at, o.ingested_at, o.value, o.run_id, o.quality
    from core.observation o
    where o.ingested_at <= p_known_at
      and (o.issued_at is null or o.issued_at <= p_known_at)
    order by o.series_id, o.interval_start, o.issued_at, o.ingested_at desc
$$;

-- migrate:down
drop function if exists core.observation_asof(timestamptz);
drop view if exists core.observation_latest;
drop table if exists core.observation;
drop table if exists core.series;
