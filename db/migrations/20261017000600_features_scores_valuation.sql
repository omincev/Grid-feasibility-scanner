-- migrate:up

create table feat.feature_value (
    feature_key    text not null,
    entity_id      text not null references ref.entity (entity_id),
    period_start   timestamptz not null,
    period_end     timestamptz not null,
    statistic      text not null default 'value',
    value          double precision,
    value_text     text,
    run_id         uuid not null,
    code_version   text not null,
    computed_at    timestamptz not null default now(),
    sensitivity    core.sensitivity not null default 'public',
    primary key (feature_key, entity_id, period_start, period_end, statistic, run_id)
);
create index feature_value_latest_idx on feat.feature_value (feature_key, entity_id, computed_at desc);

create table feat.score (
    score_kind       text not null check (score_kind in ('congestion', 'grid_friendliness')),
    entity_id        text not null references ref.entity (entity_id),
    period_start     timestamptz not null,
    period_end       timestamptz not null,
    value            double precision not null check (value between 0 and 100),
    breakdown        jsonb not null,
    weights_set_id   text not null references ref.assumption_set (set_id),
    run_id           uuid not null,
    code_version     text not null,
    computed_at      timestamptz not null default now(),
    sensitivity      core.sensitivity not null default 'public',
    primary key (score_kind, entity_id, period_start, period_end, run_id)
);

create table feat.opportunity (
    entity_id      text not null references ref.entity (entity_id),
    asset_type     text not null check (asset_type in ('bess', 'dsf', 'modular_dc')),
    matched        boolean not null,
    status         text not null check (status in ('match', 'candidate_needs_site_data', 'no_match')),
    reasons        jsonb not null,
    rules_set_id   text not null references ref.assumption_set (set_id),
    run_id         uuid not null,
    computed_at    timestamptz not null default now(),
    sensitivity    core.sensitivity not null default 'public',
    primary key (entity_id, asset_type, run_id)
);

create table val.run (
    valuation_run_id   uuid primary key,
    entity_id          text not null references ref.entity (entity_id),
    asset_type         text not null,
    template_set_id    text not null references ref.assumption_set (set_id),
    finance_set_id     text references ref.assumption_set (set_id),
    scenario_set_id    text references ref.assumption_set (set_id),
    method             text not null,
    method_version     text not null,
    price_window_start timestamptz not null,
    price_window_end   timestamptz not null,
    inputs             jsonb not null,
    labels             text[] not null default '{}',
    run_id             uuid not null,
    computed_at        timestamptz not null default now(),
    sensitivity        core.sensitivity not null default 'public'
);

create table val.result (
    valuation_run_id  uuid not null references val.run (valuation_run_id),
    metric            text not null,
    low               double precision,
    mid               double precision,
    high              double precision,
    unit              text not null,
    sensitivity       core.sensitivity not null default 'public',
    primary key (valuation_run_id, metric),
    check (low is null or high is null or low <= high)
);

-- migrate:down
drop table if exists val.result;
drop table if exists val.run;
drop table if exists feat.opportunity;
drop table if exists feat.score;
drop table if exists feat.feature_value;
