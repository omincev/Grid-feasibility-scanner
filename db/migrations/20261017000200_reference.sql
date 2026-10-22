-- migrate:up
-- Reference data, loaded from git by scripts/load_curated.py (ADR 0006).

create table ref.country (
    code          text primary key check (code ~ '^[a-z]{2}$'),
    name          text not null,
    status        text not null check (status in ('active', 'draft', 'reference')),
    timezone      text not null,
    currency      text not null,
    tso           text,
    config        jsonb not null,
    source_path   text not null,
    content_hash  text not null,
    loaded_at     timestamptz not null default now()
);

create table ref.entity (
    entity_id     text primary key check (entity_id ~ '^[a-z]{2,3}\.[a-z_]+\.[a-z0-9_.-]+$'),
    kind          text not null check (kind in ('country', 'zone', 'tso_area', 'dso_area', 'substation',
                                                'transformer', 'line', 'municipality', 'plant', 'site')),
    country_code  text not null references ref.country (code),
    parent_id     text references ref.entity (entity_id),
    name          text not null,
    eic           text,
    attributes    jsonb not null default '{}',
    sensitivity   core.sensitivity not null default 'public',
    source_path   text,
    loaded_at     timestamptz not null default now()
);
create index entity_parent_idx on ref.entity (parent_id);
create unique index entity_eic_idx on ref.entity (eic) where eic is not null;

create table ref.entity_alias (
    entity_id     text not null references ref.entity (entity_id),
    source        text not null,
    source_id     text,
    source_name   text not null,
    approved_by   text not null,
    approved_on   date not null,
    evidence      jsonb,
    primary key (source, source_name)
);

create table ref.source (
    source        text primary key,
    name          text not null,
    dossier_path  text,
    licence       text,
    terms_url     text
);

create table ref.equivalence_class (
    class         text primary key,
    dimension     text not null,
    description   text not null
);

create table ref.codebook_entry (
    entry_id           text primary key,
    source             text not null,
    dataset            text not null,
    country_code       text not null references ref.country (code),
    field              text not null,
    dimensions         jsonb not null default '{}',
    equivalence_class  text not null references ref.equivalence_class (class),
    kind               core.series_kind not null,
    unit               text not null,
    sign_convention    text,
    resample           text not null check (resample in ('sum', 'mean', 'first', 'last', 'none')),
    issued_at_rule     jsonb,
    definition         jsonb not null,
    notes              text,
    status             text not null check (status in ('draft', 'approved')),
    verified_by        text,
    verified_on        date,
    sensitivity        core.sensitivity not null default 'public',
    source_path        text not null,
    content_hash       text not null
);

create table ref.market_rule (
    rule_id       bigserial primary key,
    country_code  text not null references ref.country (code),
    zone_id       text references ref.entity (entity_id),
    rule_key      text not null,
    value         jsonb not null,
    valid_from    date,
    valid_to      date,
    precision     text not null default 'day' check (precision in ('day', 'month')),
    source        text,
    verified      boolean not null,
    note          text,
    source_path   text not null,
    check (valid_to is null or valid_from is null or valid_to > valid_from)
);
create index market_rule_lookup_idx on ref.market_rule (country_code, rule_key, valid_from);

create table ref.assumption_set (
    set_id        text primary key,
    kind          text not null,
    status        text not null check (status in ('proposed', 'approved')),
    owners        text[] not null,
    approved_by   text,
    approved_on   date,
    parameters    jsonb not null,
    sensitivity   core.sensitivity not null default 'public',
    source_path   text not null,
    content_hash  text not null,
    loaded_at     timestamptz not null default now()
);

create table ref.benchmark (
    benchmark_id  text primary key,
    entity_id     text references ref.entity (entity_id),
    zone_id       text references ref.entity (entity_id),
    description   text not null,
    expected      jsonb not null,
    owner         text not null,
    sensitivity   core.sensitivity not null default 'public',
    source_path   text not null
);

-- migrate:down
drop table if exists ref.benchmark;
drop table if exists ref.assumption_set;
drop table if exists ref.market_rule;
drop table if exists ref.codebook_entry;
drop table if exists ref.equivalence_class;
drop table if exists ref.source;
drop table if exists ref.entity_alias;
drop table if exists ref.entity;
drop table if exists ref.country;
