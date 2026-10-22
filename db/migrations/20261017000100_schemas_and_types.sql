-- migrate:up
create schema if not exists ref;
create schema if not exists core;
create schema if not exists feat;
create schema if not exists val;
create schema if not exists ops;
create schema if not exists serving;

-- Ordered: comparisons and max() give the most restrictive class (ADR 0008).
create type core.sensitivity as enum ('public', 'licensed', 'nda', 'customer', 'personal');
create type core.series_kind as enum ('actual', 'forecast', 'settlement', 'capacity');
create type core.quality as enum ('ok', 'quarantined');
create type core.claim_status as enum ('auto', 'pending_review', 'approved', 'rejected');
create type core.extraction_method as enum ('parser', 'llm', 'manual');

-- migrate:down
drop schema if exists serving cascade;
drop schema if exists ops cascade;
drop schema if exists val cascade;
drop schema if exists feat cascade;
drop schema if exists core cascade;
drop schema if exists ref cascade;
