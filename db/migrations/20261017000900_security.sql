-- migrate:up
-- Roles and row-level security (ADR 0008, docs/security.md).
-- On Supabase, anon and authenticated already exist; locally they are created here.

do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'anon') then create role anon nologin; end if;
    if not exists (select 1 from pg_roles where rolname = 'authenticated') then create role authenticated nologin; end if;
    if not exists (select 1 from pg_roles where rolname = 'scanner_ingest') then create role scanner_ingest nologin; end if;
    if not exists (select 1 from pg_roles where rolname = 'scanner_curator') then create role scanner_curator nologin; end if;
    if not exists (select 1 from pg_roles where rolname = 'scanner_analyst') then create role scanner_analyst nologin; end if;
end
$$;

-- Serving: the only surface for anonymous and logged-in front-end users.
grant usage on schema serving to anon, authenticated, scanner_analyst;
grant select on all tables in schema serving to anon, authenticated, scanner_analyst;
alter default privileges in schema serving grant select on tables to anon, authenticated, scanner_analyst;
revoke all on schema ref, core, feat, val, ops from anon, authenticated;

-- Runtime writer.
grant usage on schema ref, core, feat, val, ops to scanner_ingest;
grant select on all tables in schema ref to scanner_ingest;
grant select, insert, update on all tables in schema core, feat, val, ops to scanner_ingest;
grant usage on all sequences in schema core, feat, val, ops to scanner_ingest;

-- Curated-file loader.
grant usage on schema ref to scanner_curator;
grant select, insert, update, delete on all tables in schema ref to scanner_curator;
grant usage on all sequences in schema ref to scanner_curator;

-- Analysts read everything up to nda.
grant usage on schema ref, core, feat, val, ops to scanner_analyst;
grant select on all tables in schema ref, core, feat, val, ops to scanner_analyst;

-- RLS on every table carrying a sensitivity column.
do $$
declare
    t record;
begin
    for t in
        select c.table_schema, c.table_name
        from information_schema.columns c
        join information_schema.tables tb
          on tb.table_schema = c.table_schema and tb.table_name = c.table_name and tb.table_type = 'BASE TABLE'
        where c.column_name = 'sensitivity'
          and c.table_schema in ('ref', 'core', 'feat', 'val')
    loop
        execute format('alter table %I.%I enable row level security', t.table_schema, t.table_name);
        execute format('create policy ingest_all on %I.%I to scanner_ingest, scanner_curator using (true) with check (true)',
                       t.table_schema, t.table_name);
        execute format('create policy analyst_read on %I.%I for select to scanner_analyst using (sensitivity <= ''nda'')',
                       t.table_schema, t.table_name);
    end loop;
end
$$;

-- Observations inherit sensitivity from their series.
alter table core.observation enable row level security;
create policy ingest_all on core.observation to scanner_ingest using (true) with check (true);
create policy analyst_read on core.observation for select to scanner_analyst
    using (exists (select 1 from core.series s where s.series_id = core.observation.series_id and s.sensitivity <= 'nda'));

-- migrate:down
-- Policies and grants are dropped with their tables; roles are kept deliberately.
