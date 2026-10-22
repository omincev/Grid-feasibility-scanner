-- migrate:up
-- Sparse knowledge (ADR 0003, 0007, 0009).

create table core.source_document (
    document_id   text primary key,
    source        text not null,
    url           text,
    title         text,
    fetched_at    timestamptz not null,
    sha256        text not null,
    raw_uri       text not null,
    content_type  text not null,
    sensitivity   core.sensitivity not null default 'public'
);
create index source_document_sha_idx on core.source_document (sha256);

create table core.claim (
    claim_id      text primary key,
    document_id   text not null references core.source_document (document_id),
    attribute     text not null,
    entity_id     text references ref.entity (entity_id),
    value_num     double precision,
    value_text    text,
    unit          text,
    valid_from    date,
    valid_to      date,
    quote         text,
    page          integer,
    location      jsonb,
    method        core.extraction_method not null,
    model         text,
    prompt_hash   text,
    status        core.claim_status not null,
    reviewed_by   text,
    reviewed_on   date,
    sensitivity   core.sensitivity not null default 'public',
    created_at    timestamptz not null default now(),
    check (value_num is not null or value_text is not null),
    check (method <> 'llm' or (model is not null and prompt_hash is not null))
);

create table core.fact (
    fact_id             bigserial primary key,
    entity_id           text not null references ref.entity (entity_id),
    attribute           text not null,
    value_num           double precision,
    value_text          text,
    value_json          jsonb,
    unit                text,
    valid_from          timestamptz,
    valid_to            timestamptz,
    known_at            timestamptz not null,
    source_document_id  text references core.source_document (document_id),
    claim_id            text references core.claim (claim_id),
    run_id              uuid,
    sensitivity         core.sensitivity not null default 'public',
    check (num_nonnulls(value_num, value_text, value_json) = 1),
    check (source_document_id is not null or claim_id is not null),
    check (valid_to is null or valid_from is null or valid_to > valid_from)
);
create index fact_entity_attr_idx on core.fact (entity_id, attribute, known_at desc);

create table core.estimate (
    estimate_id     bigserial primary key,
    entity_id       text not null references ref.entity (entity_id),
    measure         text not null,
    period_start    timestamptz not null,
    period_end      timestamptz not null,
    low             double precision,
    mid             double precision,
    high            double precision,
    unit            text not null,
    method          text not null,
    method_version  text not null,
    inputs          jsonb not null,
    run_id          uuid not null,
    computed_at     timestamptz not null default now(),
    sensitivity     core.sensitivity not null default 'public',
    check (low is null or high is null or low <= high)
);
create index estimate_lookup_idx on core.estimate (entity_id, measure, computed_at desc);

-- migrate:down
drop table if exists core.estimate;
drop table if exists core.fact;
drop table if exists core.claim;
drop table if exists core.source_document;
