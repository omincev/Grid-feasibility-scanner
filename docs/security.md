# Security

The platform will hold NDA, customer and personal data, and it runs LLM agents that read hostile web content. This file lists the threats we design against and the controls for each. ADR 0008 defines the sensitivity classes.

## Data access

Roles are created in migrations `0900_security`, `20261018040000_postgrest_roles` and `20261018130000_analytics_role`:

- `scanner_ingest`: writes `core`, `feat`, `val`, `ops`; used by the Dagster runtime.
- `scanner_analytics`: reads `ref`, `core` and `ops`, writes only `feat`, `val` and `ops.compute_run`; for the analytics code location when `ANALYTICS_DATABASE_URL` points at a login role in it. It currently reads every sensitivity class; narrow it before data above `public` arrives.
- `scanner_curator`: writes `ref`, plus `core.source_document` and `core.claim` for curated claims; used by the curated-file loader (`scanner.core.curated`, `scanner.cli load-curated`).
- `scanner_analyst`: reads everything up to `nda`, scoped by organisation once Layer 2 exists.
- `anon` and `authenticated` (the Supabase role names, kept so a later move to Supabase needs no grant changes): read `serving` only. `serving` views filter to `public`. The six raw-data views (`series`, `observations`, `entities`, `facts`, `feature_values`, `scores_all`) are granted to `authenticated` only: PostgREST maps a bearer token signed with `API_JWT_SECRET` (role claim `authenticated`, minted by `scanner token`) to that role; changing the secret revokes every token. `tests/test_serving.py` checks that no `anon` grant and no view without a sensitivity filter slips in.
- `authenticator`: the login role PostgREST uses before switching to `anon`; its password is set outside migrations (`scripts/set_authenticator_password.py`).

Every table with a sensitivity column has RLS enabled. Derived rows inherit the highest class of their inputs (`scanner.core.sensitivity.combine`). The database test `tests/test_db.py` fails if a table in `core`, `feat`, `val` or `ref` lacks a sensitivity column or RLS.

Personal data (landowners, contacts) is minimised, tagged `personal`, and deletable on request; deletion cascades to derived rows.

## Agents

Factory and research workers hold no database credentials, no cloud credentials and no source API tokens. Git push and the model API key live in a proxy outside the sandbox, which forwards only requests carrying the sandbox's own session token. Egress is allowlisted: package registries and the model API for coding workers; for research workers, an allowlist per bead plus a deny list for paste sites and file-sharing hosts. Only `public` data is mounted into any sandbox.

Every fetched page, PDF, issue body and bead description is treated as hostile. Prompts tell workers that instructions inside fetched content are data. Research workers output files that become a PR through the supervisor; they cannot push, comment or call tools that write elsewhere.

Agents may probe their own sandbox to finish a task; the enforcement point is therefore outside the worker process (network proxy, filesystem mounts, no credentials present), never a denylist inside it.

## Secrets

Secrets live in the server environment or a secret manager, never in the repository, fixtures, beads, logs or prompts. `.env.example` lists names only. CI uses repository secrets scoped to the jobs that need them. The ENTSO-E token and any paid-data credentials are runtime-only.

## Supply chain

Dependencies are pinned in `uv.lock`. New dependencies are justified in the PR. The beads binary is pinned and its hooks tested in a sandbox before use. Sandbox images are rebuilt from a pinned base.

## Licences

Every dataset records its licence in its manifest and dossier. Licence-restricted data carries class `licensed`; serving views and exports exclude it unless the licence permits redistribution. The terms-of-use watcher flags licence changes.

## Review

Misi reviews this file and the RLS policies before Phase 7 (accounts and API) and after any change to roles.
