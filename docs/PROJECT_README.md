# Grid Opportunity Scanner

Vorkon's tool for finding where in the grid a flexible asset makes money, which asset fits, and roughly what it earns. It starts with public Danish data and is built to take on Italy, Hungary and any country covered by ENTSO-E.

The repository holds three things: the ingestion runtime (Dagster, Postgres with PostGIS, object storage; one docker compose stack, locally or on a single VPS), the analytics and valuation code that runs on top of it, and the human-curated definitions everything depends on (codebook, country files, market rules, claims, assumptions). Agents in a software factory build and repair most of the code; people own the definitions and approve every merge.

Start here:

- `MASTER_PLAN.md` scope, phases, tooling, gates, open decisions
- `AGENTS.md` repository map and rules for agents
- `docs/architecture.md` how the parts fit together
- `docs/adr/` decisions

Quick start:

```sh
cp .env.example .env
make setup
docker compose up -d db
make migrate
make test
make dev
```

Windows hosts without `make`, `psql` or `dbmate` (PowerShell or Git Bash):

```sh
cp .env.example .env                      # PowerShell: Copy-Item .env.example .env
uv sync --all-groups
docker compose up -d db postgrest
uv run python scripts/migrate_local.py postgresql://scanner:scanner@localhost:5432/scanner
uv run python scripts/set_authenticator_password.py postgresql://scanner:scanner@localhost:5432/scanner
uv run pytest
uv run dagster dev -w workspace.yaml        # code locations: ingestion, analytics, ops
uv run python -m scanner.cli pipeline     # --all-scenarios; or load-curated, ingest, snapshot, features, scores, valuation, export, report
uv run python -m scanner.cli backup       # pg_dump through the db container; restore <dump> --database <new scratch db>
```

Full stack, URLs, backups and moving it (`raw-uris migrate` for raw references): `infra/README.md`, section "Local stack".

Licence of the code: proprietary. Licences of the data: per source, recorded in each dossier and manifest.
