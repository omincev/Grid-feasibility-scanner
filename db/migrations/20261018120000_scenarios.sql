-- migrate:up
-- Analytics scenarios and method cards (assumptions/scenarios/README.md, docs/methods/README.md).
--
-- * ref.scenario mirrors assumptions/scenarios/*.yaml, ref.method_card mirrors docs/methods/*.md (front matter
--   and body), both loaded by scanner.core.curated. At most one scenario is official (deferred, so a load that
--   moves the status from one scenario to another commits).
-- * Score and valuation runs name their scenario (ops.compute_run.scenario_id); feature runs stay null.
-- * Every row of feat.score, feat.opportunity, val.run and val.result records the scenario, the content hashes
--   of the scenario and of its assumption sets ({set_id: hash}) and the method versions ({method_id: version},
--   the row's method and those upstream of it); feat.feature_value records its method. Rows written before
--   this migration keep nulls.

create table ref.scenario (
    scenario_id      text primary key,
    title            text not null,
    status           text not null check (status in ('official', 'exploratory')),
    description      text not null,
    owners           text[] not null,
    approved_by      text,
    approved_on      date,
    sets             jsonb not null,               -- role -> assumption set ID
    method_switches  jsonb not null default '{}',  -- "<role>.<parameter>" -> value
    overrides        jsonb not null default '{}',  -- "<role>.<parameter>" -> value
    sensitivity      core.sensitivity not null default 'public',
    source_path      text not null,
    content_hash     text not null,
    loaded_at        timestamptz not null default now(),
    constraint one_official_scenario exclude using btree (status with =) where (status = 'official')
        deferrable initially deferred
);

create table ref.method_card (
    method_id     text primary key,
    version       integer not null check (version >= 1),
    title         text not null,
    status        text not null check (status in ('provisional', 'approved')),
    owner         text[] not null,
    inputs        text[] not null,
    parameters    text[] not null,   -- <role>.<parameter>
    outputs       text[] not null,
    known_biases  text[] not null,
    body          text not null,     -- markdown
    sensitivity   core.sensitivity not null default 'public',
    source_path   text not null,
    content_hash  text not null,
    loaded_at     timestamptz not null default now()
);

-- The 0900 policy loop has already run, so policies and grants are explicit here.
alter table ref.scenario enable row level security;
create policy ingest_all on ref.scenario to scanner_ingest, scanner_curator using (true) with check (true);
create policy analyst_read on ref.scenario for select to scanner_analyst using (sensitivity <= 'nda');
alter table ref.method_card enable row level security;
create policy ingest_all on ref.method_card to scanner_ingest, scanner_curator using (true) with check (true);
create policy analyst_read on ref.method_card for select to scanner_analyst using (sensitivity <= 'nda');
grant select on ref.scenario, ref.method_card to scanner_ingest, scanner_analyst;
grant select, insert, update, delete on ref.scenario, ref.method_card to scanner_curator;

alter table ops.compute_run add column scenario_id text references ref.scenario (scenario_id);
create index compute_run_scenario_idx on ops.compute_run (scenario_id, kind, name, started_at desc);

alter table feat.feature_value
    add column method_id text,
    add column method_version text;

alter table feat.score
    add column scenario_id text references ref.scenario (scenario_id),
    add column scenario_hash text,
    add column assumption_hashes jsonb,
    add column method_id text,
    add column method_version text,
    add column method_versions jsonb;

alter table feat.opportunity
    add column scenario_id text references ref.scenario (scenario_id),
    add column scenario_hash text,
    add column assumption_hashes jsonb,
    add column method_id text,
    add column method_version text,
    add column method_versions jsonb;

-- val.run already has method and method_version.
alter table val.run
    add column scenario_id text references ref.scenario (scenario_id),
    add column scenario_hash text,
    add column assumption_hashes jsonb,
    add column method_versions jsonb;

alter table val.result
    add column scenario_id text references ref.scenario (scenario_id),
    add column scenario_hash text,
    add column assumption_hashes jsonb,
    add column method_id text,
    add column method_version text,
    add column method_versions jsonb;

create index score_run_idx on feat.score (run_id);
create index opportunity_run_idx on feat.opportunity (run_id);
create index val_run_run_idx on val.run (run_id);

-- migrate:down
drop index if exists val.val_run_run_idx;
drop index if exists feat.opportunity_run_idx;
drop index if exists feat.score_run_idx;
alter table val.result
    drop column if exists method_versions, drop column if exists method_version, drop column if exists method_id,
    drop column if exists assumption_hashes, drop column if exists scenario_hash, drop column if exists scenario_id;
alter table val.run
    drop column if exists method_versions, drop column if exists assumption_hashes,
    drop column if exists scenario_hash, drop column if exists scenario_id;
alter table feat.opportunity
    drop column if exists method_versions, drop column if exists method_version, drop column if exists method_id,
    drop column if exists assumption_hashes, drop column if exists scenario_hash, drop column if exists scenario_id;
alter table feat.score
    drop column if exists method_versions, drop column if exists method_version, drop column if exists method_id,
    drop column if exists assumption_hashes, drop column if exists scenario_hash, drop column if exists scenario_id;
alter table feat.feature_value drop column if exists method_version, drop column if exists method_id;
drop index if exists ops.compute_run_scenario_idx;
alter table ops.compute_run drop column if exists scenario_id;
drop table if exists ref.method_card;
drop table if exists ref.scenario;
