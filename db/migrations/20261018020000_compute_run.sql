-- migrate:up
-- Provenance for derived data (scanner.runtime.provenance). Every run_id in feat.feature_value,
-- feat.score, feat.opportunity and val.run resolves to a row here. Like the other ops tables it has
-- no sensitivity column: it holds run metadata, never data values.

create table ops.compute_run (
    run_id        uuid primary key,
    kind          text not null check (kind in ('feature', 'score', 'valuation')),
    name          text not null,
    started_at    timestamptz not null default now(),
    finished_at   timestamptz,
    -- partial: some outputs were not computed; skipped: none were. The reason is in error.
    status        text not null check (status in ('running', 'ok', 'partial', 'skipped', 'failed')),
    code_version  text not null,
    params        jsonb not null default '{}',
    error         text
);
create index compute_run_lookup_idx on ops.compute_run (kind, name, started_at desc);

-- NOT VALID: enforced for every new row, without scanning rows written before this migration.
alter table feat.feature_value
    add constraint feature_value_run_fk foreign key (run_id) references ops.compute_run (run_id) not valid;
alter table feat.score
    add constraint score_run_fk foreign key (run_id) references ops.compute_run (run_id) not valid;
alter table feat.opportunity
    add constraint opportunity_run_fk foreign key (run_id) references ops.compute_run (run_id) not valid;
alter table val.run
    add constraint val_run_run_fk foreign key (run_id) references ops.compute_run (run_id) not valid;

-- The 0900 grants covered only the tables that existed then.
grant select, insert, update on ops.compute_run to scanner_ingest;
grant select on ops.compute_run to scanner_analyst;

-- migrate:down
alter table val.run drop constraint if exists val_run_run_fk;
alter table feat.opportunity drop constraint if exists opportunity_run_fk;
alter table feat.score drop constraint if exists score_run_fk;
alter table feat.feature_value drop constraint if exists feature_value_run_fk;
drop table if exists ops.compute_run;
