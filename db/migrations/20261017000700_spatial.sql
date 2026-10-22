-- migrate:up
-- Requires PostGIS (enabled on Supabase in WP0.1; local image postgis/postgis:16-3.4).
create extension if not exists postgis;

create table ref.entity_geometry (
    entity_id     text primary key references ref.entity (entity_id),
    geom          geometry(Geometry, 4326) not null,
    source        text not null,
    blob_id       text,
    approved_by   text,
    sensitivity   core.sensitivity not null default 'public',
    loaded_at     timestamptz not null default now()
);
create index entity_geometry_gix on ref.entity_geometry using gist (geom);

-- migrate:down
drop table if exists ref.entity_geometry;
