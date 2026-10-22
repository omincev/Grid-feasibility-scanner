-- migrate:up
-- Runtime operations: runs, fingerprints, snapshots, quarantine decisions, bead outbox.

create table ops.ingest_run (
    run_id           uuid primary key,
    source           text not null,
    dataset          text not null,
    partition_key    text,
    window_start     timestamptz,
    window_end       timestamptz,
    started_at       timestamptz not null default now(),
    finished_at      timestamptz,
    status           text not null check (status in ('running', 'ok', 'blocked', 'failed')),
    rows_received    integer,
    rows_expected    integer,
    rows_written     integer,
    raw_uris         text[] not null default '{}',
    code_version     text,
    error            text
);
create index ingest_run_lookup_idx on ops.ingest_run (source, dataset, started_at desc);

create table ops.fingerprint (
    run_id       uuid not null references ops.ingest_run (run_id),
    payload      jsonb not null,
    deviations   jsonb not null default '[]',
    severity     text not null check (severity in ('none', 'soft', 'hard')),
    created_at   timestamptz not null default now(),
    primary key (run_id)
);

create table ops.snapshot (
    snapshot_id   text not null,
    fetched_at    timestamptz not null default now(),
    url           text not null,
    status_code   integer,
    sha256        text,
    raw_uri       text,
    changed       boolean not null,
    error         text,
    primary key (snapshot_id, fetched_at)
);

create table ops.quarantine_decision (
    decision_id   bigserial primary key,
    run_id        uuid not null references ops.ingest_run (run_id),
    decided_by    text not null,
    decision      text not null check (decision in ('accept', 'reject')),
    reason        text not null,
    decided_at    timestamptz not null default now()
);

-- Runtime to factory (ADR 0005). Never copy sensitive content into title or body; link raw_uri.
create table ops.bead_outbox (
    outbox_id     bigserial primary key,
    kind          text not null check (kind in ('adapter-fix', 'parser-fix', 'research-source',
                                                'research-evidence', 'human')),
    title         text not null,
    body          text not null,
    raw_uri       text,
    source        text,
    dataset       text,
    created_at    timestamptz not null default now(),
    drained_at    timestamptz,
    bead_id       text
);
create index bead_outbox_pending_idx on ops.bead_outbox (created_at) where drained_at is null;

-- migrate:down
drop table if exists ops.bead_outbox;
drop table if exists ops.quarantine_decision;
drop table if exists ops.snapshot;
drop table if exists ops.fingerprint;
drop table if exists ops.ingest_run;
