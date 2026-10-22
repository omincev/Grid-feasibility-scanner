-- migrate:up
-- Database role of the analytics code location (scanner.analytics_definitions; infra/README.md, "Analytics role").
--
-- scanner_analytics (nologin) reads ref, core, ops, feat and val, and writes only what feature, score,
-- opportunity and valuation runs write: rows in feat and val, and their provenance in ops.compute_run. It never
-- writes ref (job_compute's curated load connects with DATABASE_URL), core or the other ops tables. It may switch
-- to anon, as the static export and the report do to read the serving views the way the public does.
-- Reads are not limited by sensitivity (like scanner_ingest): outputs combine their inputs' sensitivity, and a
-- hidden input would silently change a result.
--
-- A login role is a deployment step, never part of a migration (no password here), for example:
--   create role scanner_analytics_login login password '...' in role scanner_analytics;
-- then ANALYTICS_DATABASE_URL=postgresql://scanner_analytics_login:...@<host>/<db> for the analytics location.

do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'scanner_analytics') then
        create role scanner_analytics nologin;
    end if;
end
$$;

grant usage on schema ref, core, ops, feat, val to scanner_analytics;
grant select on all tables in schema ref, core, ops, feat, val to scanner_analytics;
grant insert on all tables in schema feat, val to scanner_analytics;
grant usage on all sequences in schema feat, val to scanner_analytics;
grant insert, update on ops.compute_run to scanner_analytics;
grant anon to scanner_analytics;

-- Tables and views later migrations create (as the role running this one).
alter default privileges in schema ref, core, ops, feat, val grant select on tables to scanner_analytics;
alter default privileges in schema feat, val grant insert on tables to scanner_analytics;
alter default privileges in schema feat, val grant usage on sequences to scanner_analytics;

-- The 0900 policy loop has already run: policies on every table with row-level security, explicitly.
do $$
declare
    t record;
begin
    for t in
        select n.nspname as schema_name, c.relname as table_name
        from pg_class c
        join pg_namespace n on n.oid = c.relnamespace
        where c.relkind = 'r' and c.relrowsecurity and n.nspname in ('ref', 'core', 'feat', 'val')
    loop
        execute format('create policy analytics_read on %I.%I for select to scanner_analytics using (true)',
                       t.schema_name, t.table_name);
        if t.schema_name in ('feat', 'val') then
            execute format('create policy analytics_write on %I.%I for insert to scanner_analytics with check (true)',
                           t.schema_name, t.table_name);
        end if;
    end loop;
end
$$;

-- migrate:down
do $$
declare
    p record;
begin
    for p in
        select schemaname, tablename, policyname from pg_policies
        where policyname in ('analytics_read', 'analytics_write') and schemaname in ('ref', 'core', 'feat', 'val')
    loop
        execute format('drop policy %I on %I.%I', p.policyname, p.schemaname, p.tablename);
    end loop;
end
$$;
alter default privileges in schema feat, val revoke usage on sequences from scanner_analytics;
alter default privileges in schema feat, val revoke insert on tables from scanner_analytics;
alter default privileges in schema ref, core, ops, feat, val revoke select on tables from scanner_analytics;
revoke anon from scanner_analytics;
revoke all on all tables in schema ref, core, ops, feat, val from scanner_analytics;
revoke all on all sequences in schema feat, val from scanner_analytics;
revoke usage on schema ref, core, ops, feat, val from scanner_analytics;
-- The role itself is kept, like the roles of 0900_security.
