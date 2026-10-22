-- migrate:up
-- serving.source_completeness (docs/ui-contract.md): per manifest dataset, the local days of its history up
-- to yesterday and how many of them were received in full, short, without a completeness check, or not at
-- all, from ops.ingest_run. serving.source_freshness shows the newest checked day only, so interior gaps
-- were invisible there.
--
-- A day counts by its best successful run: complete if any ok run of that partition received at least the
-- rows it expected (stored rows are never deleted, ADR 0003), incomplete if ok runs were checked and all
-- fell short, unchecked if no ok run had an expected count (a run on the day itself, or a resolution that
-- cannot be counted), missing if no run of the partition succeeded. Rows a neighbouring day's lookback or
-- lookahead wrote are not counted for the day. Today's partition is incomplete by nature and left out.
create view serving.source_completeness as
with datasets as (
    select d.source, d.dataset, d.country_code, d.status, d.history_start as first_day,
           least(coalesce(d.history_end, 'infinity'::date), (now() at time zone c.timezone)::date - 1) as last_day
    from ref.dataset d
    join ref.country c on c.code = d.country_code
    where d.kind = 'manifest' and d.sensitivity = 'public' and d.history_start is not null
),
best as (
    select distinct on (r.source, r.dataset, r.partition_key)
           r.source, r.dataset, r.partition_key, r.rows_expected, r.rows_received
    from ops.ingest_run r
    where r.status = 'ok' and r.partition_key ~ '^\d{4}-\d{2}-\d{2}$'
    order by r.source, r.dataset, r.partition_key,
             r.rows_received >= r.rows_expected desc nulls last,
             r.rows_expected is null,
             r.rows_received desc nulls last,
             r.started_at desc
),
days as (
    select d.source, d.dataset, g.day::date as day, b.rows_expected, b.rows_received,
           case when b.partition_key is null then 'missing'
                when b.rows_expected is null then 'unchecked'
                when b.rows_received >= b.rows_expected then 'complete'
                else 'incomplete' end as state
    from datasets d
    cross join lateral generate_series(d.first_day, d.last_day, interval '1 day') g(day)
    left join best b
        on b.source = d.source and b.dataset = d.dataset and b.partition_key = to_char(g.day, 'YYYY-MM-DD')
),
counted as (
    select d.source, d.dataset, d.country_code, d.status, d.first_day, d.last_day,
           count(x.day)::integer                                       as partitions_expected,
           (count(*) filter (where x.state = 'complete'))::integer     as partitions_complete,
           (count(*) filter (where x.state = 'incomplete'))::integer   as partitions_incomplete,
           (count(*) filter (where x.state = 'unchecked'))::integer    as partitions_unchecked,
           (count(*) filter (where x.state = 'missing'))::integer      as partitions_missing,
           sum(x.rows_expected)::bigint                                as rows_expected,
           (sum(x.rows_received) filter (where x.rows_expected is not null))::bigint as rows_received,
           coalesce(array_agg(x.day::text order by x.day) filter (where x.state = 'incomplete'), '{}')
               as incomplete_partitions,
           coalesce(array_agg(x.day::text order by x.day) filter (where x.state = 'missing'), '{}')
               as missing_partitions
    from datasets d
    left join days x on x.source = d.source and x.dataset = d.dataset
    group by d.source, d.dataset, d.country_code, d.status, d.first_day, d.last_day
)
select c.source,
       src.name                 as source_name,
       c.dataset,
       c.country_code,
       c.status                 as registry_status,
       c.first_day              as first_partition,
       c.last_day               as last_partition,
       c.partitions_expected,
       c.partitions_complete,
       c.partitions_incomplete,
       c.partitions_unchecked,
       c.partitions_missing,
       c.rows_expected,
       c.rows_received,
       c.incomplete_partitions,
       c.missing_partitions,
       case when c.partitions_incomplete + c.partitions_missing > 0 then false
            when c.partitions_unchecked > 0 then null
            else true end       as complete
from counted c
left join ref.source src on src.source = c.source;

-- The serving default privileges cover new views when this runs as the 0900 role; grant explicitly anyway.
grant select on serving.source_completeness to anon, authenticated, scanner_analyst;

-- migrate:down
drop view if exists serving.source_completeness;
