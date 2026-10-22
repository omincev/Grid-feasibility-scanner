-- migrate:up
-- PostgREST connection role, mirroring Supabase: authenticator logs in, then switches to anon or authenticated.
-- No password here. Locally set it with scripts/set_authenticator_password.py (infra/README.md);
-- on Supabase the role already exists and its password is managed by the platform.

do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'authenticator') then
        create role authenticator noinherit nologin;
    end if;
end
$$;

grant anon, authenticated to authenticator;

-- Serving views created by later migrations must stay readable. The default privileges in 0900 apply only
-- to objects created by the role that ran 0900; repeat them for the current migration role (idempotent)
-- and re-grant on everything that exists now.
grant usage on schema serving to anon, authenticated, scanner_analyst;
grant select on all tables in schema serving to anon, authenticated, scanner_analyst;
alter default privileges in schema serving grant select on tables to anon, authenticated, scanner_analyst;

-- migrate:down
-- Roles are kept deliberately (see 0900_security).
