-- migrate:up
-- Files loaded by the curated-file loader (scanner.core.curated, ADR 0006). content_hash lets the
-- loader skip unchanged files; keys holds the natural keys each file produced, as
-- {"<schema>.<table>": [[key, ...], ...]}, so entries removed from a file can be found.
create table ref.curated_file (
    source_path   text primary key,
    folder        text not null,
    content_hash  text not null,
    keys          jsonb not null default '{}',
    sensitivity   core.sensitivity not null default 'public',
    loaded_at     timestamptz not null default now()
);

-- The 0900 policy loop has already run, so policies and grants are explicit here.
alter table ref.curated_file enable row level security;
create policy ingest_all on ref.curated_file to scanner_ingest, scanner_curator using (true) with check (true);
create policy analyst_read on ref.curated_file for select to scanner_analyst using (sensitivity <= 'nda');
grant select on ref.curated_file to scanner_ingest, scanner_analyst;
grant select, insert, update, delete on ref.curated_file to scanner_curator;

-- The loader also writes curated claims (claims/<cc>/*.yaml) to core; RLS policies for
-- scanner_curator on these tables already exist (0900).
grant usage on schema core to scanner_curator;
grant select, insert, update, delete on core.source_document, core.claim to scanner_curator;

-- migrate:down
revoke select, insert, update, delete on core.source_document, core.claim from scanner_curator;
revoke usage on schema core from scanner_curator;
drop table if exists ref.curated_file;
