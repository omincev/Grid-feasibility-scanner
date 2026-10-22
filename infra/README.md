# Infrastructure and operations

## Local stack

Everything is configured through environment variables (`.env`, read by docker compose and by `scanner.settings`), so moving a part means repointing variables.

| Service | Profile | URL | Notes |
|---|---|---|---|
| `db` (PostGIS 16) | default | `localhost:5432` (`POSTGRES_PORT`) | user/password/db `scanner`; volume `pgdata` |
| `postgrest` v12.2.3 | default | http://localhost:3001 (`POSTGREST_PORT`) | schema `serving`, role `anon` |
| `dagster-webserver` | `orchestrator` | http://localhost:3000 (`DAGSTER_PORT`) | code locations `ingestion` and `analytics` (`workspace.yaml`); run storage in database `dagster` |
| `dagster-daemon` | `orchestrator` | none | schedules and sensors |
| `minio` | `s3` | http://localhost:9001 | S3-compatible raw store |

Start and stop (Windows without make: use the compose commands):

```sh
make up                                   # docker compose up -d db postgrest
docker compose --profile orchestrator up -d --build dagster-webserver dagster-daemon   # adds Dagster (see below)
make down                                 # stops containers, never removes volumes (no -v)
```

First-time setup after `db` is up:

```sh
uv run python scripts/migrate_local.py postgresql://scanner:scanner@localhost:5432/scanner   # make migrate-local
uv run python scripts/set_authenticator_password.py postgresql://scanner:scanner@localhost:5432/scanner
docker compose restart postgrest
curl http://localhost:3001/zone_card      # anon read of the serving schema
```

Migration `20261018040000_postgrest_roles` creates the `authenticator` role (can switch to `anon` and `authenticated`, as on Supabase) without a password. The password is a local-only step: `set_authenticator_password.py` uses `$AUTHENTICATOR_PASSWORD`, default `authenticator`. Never reuse that value anywhere real. The `serving` select grants cover views created by later migrations as long as they run as the same role as the 0900 migration; the postgrest_roles migration repeats the default privileges for the current migration role to be safe. `anon` cannot reach `ref`, `core`, `feat`, `val` or `ops`.

Dagster: the image is built from `infra/Dockerfile`; `workspace.yaml`, `src/`, `docs/` (method cards and dossiers) and the curated folders are bind-mounted, so code edits need only a container restart. Raw captures go to the host directory `RAW_STORE_HOST_DIR` (default `./.data/raw`), mounted at `/data/raw`, so they survive rebuilds and, run from the main checkout, are the same directory the host CLI writes. The static export goes to `EXPORTS_HOST_DIR` (default `./exports`). The one-shot `dagster-db-init` creates the database named in `DAGSTER_PG_URL` (default `dagster`). If the Chromium install makes the build slow, use `INSTALL_BROWSER=false` (no active adapter needs Playwright today).

### Orchestrator start-up

What keeps the platform current: every schedule starts as running in the containers (`SCANNER_SCHEDULES_DEFAULT_RUNNING=true`, set by compose; a host `dagster dev` keeps them stopped). Ingest, snapshot and capacity-map schedules (location `ingestion`) refresh the inputs; ingest schedules materialise today's partition, and each active dataset also re-ingests yesterday's at 01:30 local time (`schedule_<dataset>_yesterday`), so every completed day gets a run whose fingerprint and row count cover the whole day (`serving.source_completeness`); `schedule_compute_daily` (location `analytics`, 15:30 Europe/Copenhagen, after the day-ahead ingests) runs `job_compute` as one run, in dependency order: the curated load (`curated/analytics_load`), features (F1, F2, F4, F5, F7) once, congestion score, opportunity types and BESS return range v0 for every scenario partition, then the static export (`scanner.assets.compute`). The Monday capacity map reaches the scores through F7 in that day's compute run. `sensor_assumption_files` (running by default everywhere; it only reads files) keeps the `scenario` partitions equal to `assumptions/scenarios/*.yaml` and records new hashes of assumption files within a minute, so the assets that use a changed file show as "Unsynced"; they recompute in the next daily run (`scanner.assets.assumptions` says why). A schedule switched off in the UI stays off; its state lives in the `dagster` database. Toggles made before the split into two locations (location `scanner.definitions`) do not carry over: each schedule starts from its default again.

### Code locations

`workspace.yaml` loads three code locations, each in its own process (the third, `ops`, holds only the alert sensors: "Alerts" under "VPS" below):

- `ingestion` (`scanner.ingestion_definitions`): ingest assets, snapshots, the capacity map, the curated loader (`curated/files`) and their schedules. Each ingest asset's description and metadata show its normalisation rules: UTC time field, resolution, lookback and lookahead, the codebook entries with unit, sign convention and resampling rule, the country files' source precedence, the fingerprint thresholds, and the dossier with its "Decisions" section (`scanner.assets.rules`).
- `analytics` (`scanner.analytics_definitions`): features, scores, opportunity types, valuation, the serving export, `job_compute` and its schedule, and every assumption set, scenario file and method card as an observable source asset (data version = the file's SHA-256, the hash load-curated stores and every output row records). Scores, opportunities and valuation are partitioned by `scenario`; features are not. The ingestion assets it reads are declared as external specs with the same keys, so the UI shows one lineage from ingest to export.

A failing import in `analytics` leaves `ingestion` loaded and its schedules running: the locations load in separate processes, and the ingestion location imports no analytics module (`tests/test_definitions.py` loads it with every analytics module broken). One scenario now: open `scores/congestion` in the UI, Materialize, pick the partition; it refuses to run while `ref` holds older copies of that scenario's files, so after an assumption change launch `job_compute` (or materialize `curated/analytics_load` first). Everything now: Jobs, `job_compute`, Materialize with all partitions (one run), or the CLI command under "Inspect".

### Analytics role

Migration `20261018130000_analytics_role` creates `scanner_analytics` (nologin): it reads `ref`, `core`, `ops`, `feat` and `val`, writes only `feat`, `val` and `ops.compute_run`, and may switch to `anon` for the serving views (export and report). The analytics location's features, scores, valuation and export connect with `ANALYTICS_DATABASE_URL`, falling back to `DATABASE_URL`; the curated load in `job_compute` always uses `DATABASE_URL`, because it writes `ref`. Nothing changes until you set it. To use the role, create a login role in it (a deployment step, never a migration) and point the analytics location at it:

```sh
# once per database server, with a password of your own (Supabase: the SQL editor)
create role scanner_analytics_login login password '<password>' in role scanner_analytics;
# host CLI and dagster dev (.env), and the containers
ANALYTICS_DATABASE_URL=postgresql://scanner_analytics_login:<password>@localhost:5432/scanner
ORCH_ANALYTICS_DATABASE_URL=postgresql://scanner_analytics_login:<password>@db:5432/scanner
```

Both locations run under one webserver and daemon environment: the role limits what analytics runs can write; it is not a secret boundary between the locations.

Prerequisites: `db` is up with the `scanner` database migrated and `load-curated` done (first-time setup above), and `.env` in the main checkout sets:

- `ENTSOE_API_TOKEN` (or `ENTSOE_API_KEY`): required by the ENTSO-E ingests; passed into both containers.
- `SCANNER_ALLOW_PROPOSED_ASSUMPTIONS=true` while owner values are null, otherwise the congestion score refuses to run and the export after it is skipped.
- `DAGSTER_HOME`: only for host `dagster` commands, which read `.env`; it must be an absolute, existing directory or absent. The containers always use `/opt/dagster`.
- Optional, defaults shown in "Environment variables": `DAGSTER_PORT`, `DAGSTER_PG_URL` (must use `postgresql+psycopg2://`), `ORCH_DATABASE_URL`, `ORCH_ANALYTICS_DATABASE_URL`, `ORCH_RAW_STORE_URL`, `RAW_STORE_HOST_DIR`, `EXPORTS_HOST_DIR`, `USER_AGENT`, `INSTALL_BROWSER`.

Start, from the main checkout (Git Bash; in PowerShell use `$env:GIT_SHA = git rev-parse --short HEAD`):

```sh
export GIT_SHA=$(git rev-parse --short HEAD)    # recorded on every run as the code version
docker compose --profile orchestrator up -d --build dagster-webserver dagster-daemon
```

This creates the `dagster` database on first start and leaves a running `db` as it is. Both containers restart on their own after a Docker or host restart, and so do `db` and `postgrest` (`restart: unless-stopped`, since 2026-10-18). Containers created before that keep their old policy until they are recreated; to give them the policy without recreating them, run `docker update --restart unless-stopped grid-scanner-db-1 grid-scanner-postgrest-1`. The next `docker compose up -d db postgrest` recreates both (their configuration changed: restart policy and digest-pinned images, the same images as before), keeping the `pgdata` volume.

Inspect:

```sh
docker compose --profile orchestrator ps                               # both "healthy" after about 2 minutes
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:3000/server_info   # 200; UI at http://localhost:3000
docker compose --profile orchestrator exec dagster-daemon dagster-daemon liveness-check
docker compose --profile orchestrator exec dagster-daemon dagster schedule list -w /app/workspace.yaml -l ingestion
docker compose --profile orchestrator logs --tail 100 -f dagster-daemon
# compute now, every scenario in one run (Git Bash: prefix MSYS_NO_PATHCONV=1); partition order as the UI lists it
docker compose --profile orchestrator exec dagster-daemon dagster job launch -w /app/workspace.yaml -l analytics -j job_compute \
  --tags '{"dagster/asset_partition_range_start": "demo_default", "dagster/asset_partition_range_end": "spec_v0"}'
```

After a merge: `docker compose --profile orchestrator restart dagster-webserver dagster-daemon` picks up code (rerun the start command to record the new `GIT_SHA`; add `--build` when `uv.lock` changed). Assumption sets, scenarios and method cards are loaded by the next `job_compute` (its first step); registry, codebook and manifest changes need `uv run python -m scanner.cli load-curated` on the host or a materialization of `curated/files`. A new assumption set or method card file appears as an asset after the restart; a new scenario file needs only the sensor.

Stop (keeps `db` and `postgrest` running; `make down` would stop them too):

```sh
docker compose --profile orchestrator stop dagster-webserver dagster-daemon
```

### Environment variables

Names with local defaults (the content intended for `.env.example`; that file could not be edited by the agent that wrote this section, so check it lists the same names):

```
DATABASE_URL=postgresql://scanner:scanner@localhost:5432/scanner
ANALYTICS_DATABASE_URL=                  # optional: a login role in scanner_analytics ("Analytics role")
TEST_DATABASE_URL=postgresql://scanner:scanner@localhost:5432/scanner
RAW_STORE_URL=file://./.data/raw
RESTRICTED_CURATED_PATH=
ENTSOE_API_TOKEN=            # secret: your own .env only (ENTSOE_API_KEY is accepted instead)
ANTHROPIC_API_KEY=           # secret: your own .env only
HTTP_TIMEOUT_S=60
USER_AGENT=grid-scanner/0.1 (+contact: set in .env)
SCANNER_ALLOW_PROPOSED_ASSUMPTIONS=false
POSTGRES_PORT=5432
POSTGREST_PORT=3001
POSTGREST_DB_HOST=db
POSTGREST_DB_PORT=5432
POSTGREST_DB_NAME=scanner
AUTHENTICATOR_PASSWORD=authenticator      # local only
DAGSTER_PORT=3000
DAGSTER_PG_URL=postgresql+psycopg2://scanner:scanner@db:5432/dagster
ORCH_DATABASE_URL=postgresql://scanner:scanner@db:5432/scanner   # DATABASE_URL as seen inside containers
ORCH_ANALYTICS_DATABASE_URL=              # ANALYTICS_DATABASE_URL inside containers; empty: ORCH_DATABASE_URL
ORCH_RAW_STORE_URL=file:///data/raw
RAW_STORE_HOST_DIR=./.data/raw            # same directory as RAW_STORE_URL on the host
EXPORTS_HOST_DIR=./exports
ORCH_SCHEDULES_DEFAULT_RUNNING=true       # containers only; never set SCANNER_SCHEDULES_DEFAULT_RUNNING in .env
DAGSTER_IMAGE=grid-scanner-dagster:local
INSTALL_BROWSER=true
GIT_SHA=local
DB_CONTAINER=grid-scanner-db-1            # scanner.cli backup / restore run pg_dump and pg_restore in it
BACKUP_DIR=.data/backups                  # relative to the directory the command runs in
BACKUP_KEEP=7
```

### Backups

`scanner.cli backup` (Dagster-independent; `scanner.runtime.backup`) dumps `DATABASE_URL`'s database with `pg_dump -Fc` inside the `db` container (`docker exec`, so the host needs no PostgreSQL client), writes `<BACKUP_DIR>/<database>-<UTC yyyymmddThhmmssZ>.dump`, keeps it only if `pg_restore --list` reads it back, then deletes all but the newest `BACKUP_KEEP` dumps of that database. A failed dump leaves no file and deletes nothing. Run from the main checkout (Git Bash or PowerShell):

```sh
uv run python -m scanner.cli backup                                   # .data/backups, newest 7 kept
uv run python -m scanner.cli backup --dir D:/backups/scanner --keep 14  # another disk is better
```

Restore test (also the restore itself): `restore` creates a new scratch database in the same container and loads the dump with `pg_restore --no-owner --exit-on-error`; it refuses a database that exists, so it cannot overwrite `scanner`. Compare, then drop the scratch database:

```sh
uv run python -m scanner.cli restore .data/backups/scanner-20261018T150000Z.dump --database scanner_restore_check
for db in scanner scanner_restore_check; do   # Git Bash; psql runs in the container
  docker exec grid-scanner-db-1 psql -U scanner -d $db -tAc "select count(*) from core.observation"
done
docker exec grid-scanner-db-1 dropdb -U scanner scanner_restore_check
```

Without Python: `docker exec grid-scanner-db-1 pg_dump -U scanner -Fc -d scanner > scanner.dump` (Git Bash; byte-exact) and, into a new database, `docker exec grid-scanner-db-1 createdb -U scanner scanner_restore_check` then `docker exec -i grid-scanner-db-1 pg_restore -U scanner -d scanner_restore_check --no-owner --exit-on-error < scanner.dump`. Roles are cluster-wide and not in the dump; on another server create them first (the migrations' role statements, `set_authenticator_password.py`). The raw store is not in the dump: back up `RAW_STORE_HOST_DIR` as files. On the VPS a systemd timer runs the backup nightly and a restore check weekly ("Backups on the VPS"); on the laptop, Windows Task Scheduler can run the `backup` command.

### Moving parts

- Database: change `DATABASE_URL` (host-side CLI), `ORCH_DATABASE_URL` and `DAGSTER_PG_URL` (containers), and `POSTGREST_DB_HOST/PORT/NAME` (PostgREST). Move the data with `pg_dump -Fc` and `pg_restore`:
  ```sh
  docker compose exec -T db pg_dump -U scanner -Fc scanner > scanner.dump
  docker compose exec -T db pg_restore -U scanner -d scanner --clean --if-exists --no-owner < scanner.dump
  ```
  (second command run against the new server; roles are cluster-wide, so rerun the migrations' role step or `set_authenticator_password.py` there).
- Raw store: copy the directory `RAW_STORE_HOST_DIR` (everything under it), or point it elsewhere, and set `RAW_STORE_URL` and `ORCH_RAW_STORE_URL` to the new root. Captures are laid out `raw/<source>/<dataset>/<yyyy>/<mm>/<dd>/<run_id>-<seq>.<ext>` under it, and the database refers to them relative to that root (`raw://<source>/<dataset>/...`, `scanner.core.raw_store`): the host CLI and the containers resolve the same references through their own `RAW_STORE_URL`, and nothing in the database changes when the store moves. References written before 2026-10-18 were absolute and differed by writer (`file://C:/.../.data/raw/raw/...` from the host CLI, `file:///data/raw/raw/...` from the containers); rewrite them once with `uv run python -m scanner.cli raw-uris migrate` (a dry run listing counts and the roots found; `--verify` also checks that every reference resolves in `RAW_STORE_URL`; `--yes` writes, in one transaction; a second run changes nothing).
- Orchestrator: it is stateless besides the `dagster` database. Production runs the whole stack on one VPS ("VPS" below).
- S3: start `docker compose --profile s3 up -d minio`, create bucket `raw`, set `RAW_STORE_URL=s3://raw/prefix` and `ORCH_RAW_STORE_URL` likewise, plus the endpoint and credentials: `FSSPEC_S3_ENDPOINT_URL=http://localhost:9000` (fsspec's environment config, which s3fs reads), `AWS_ACCESS_KEY_ID=minio`, `AWS_SECRET_ACCESS_KEY=minio-secret`. Existing local captures must be copied to the bucket with the same relative paths.
- Supabase later: set `DATABASE_URL` to the Supabase connection string and apply migrations with dbmate. `anon`, `authenticated` and `authenticator` already exist there (the migrations skip creating them); drop the local `postgrest` service and expose `serving` through the Supabase Data API, which does the same job. Passwords for roles are managed by Supabase; do not run `set_authenticator_password.py`. Replace `ORCH_RAW_STORE_URL` with an object-storage URL.

## VPS

Decision (project owner, 2026-10-18): production runs on one Hetzner Cloud **CX33** with Ubuntu 24.04 LTS, everything in docker compose. Hetzner's page lists the CX33 as 4 shared vCPU (x86, Intel or AMD), 8 GB RAM, **80 GB NVMe**, 20 TB traffic; on 2026-10-18 it also said "currently unavailable". The Arm CAX21 has the same size but needs an arm64 image (the image workflow builds amd64 only); a CPX plan is x86.

Files: `infra/docker-compose.prod.yml` (its header says why it is a file of its own rather than profiles in `docker-compose.yml`), `infra/Caddyfile`, `infra/minio-init.sh`, `infra/bootstrap.sh`, `infra/deploy.sh`, `infra/ci-deploy.sh`, `infra/backup.sh`, `infra/restore-check.sh`, the wrappers `compose.sh`, `scanner.sh`, `mc.sh` and their `lib.sh`, `infra/systemd/`, `infra/server-env.template`; `scripts/deploy_checks.py` and `scripts/offsite_copy.py`; `.github/workflows/image.yml`.

### Layout

| Service | Reachable | Data |
|---|---|---|
| `caddy` (2.10.2) | public 80 and 443: `https://$DOMAIN/` is PostgREST, `https://$DOMAIN/exports/` the static export; anything but GET, HEAD and OPTIONS gets 405; `https://dagster.$DOMAIN/` is the Dagster UI behind basic auth | volumes `caddy_data` (certificates), `caddy_config` |
| `postgrest` (v12.2.3) | compose network only, through Caddy | none |
| `db` (PostGIS 16) | compose network only, no published port | volume `pgdata` |
| `minio` (raw store) | S3 API on the compose network only; console on `127.0.0.1:9001` | volume `miniodata` |
| `dagster-webserver`, `dagster-daemon` | UI on `127.0.0.1:3000`, and through Caddy at `https://dagster.$DOMAIN/` with a password | volume `dagster_storage` (compute logs); run storage in database `dagster` |
| `minio-init`, `dagster-db-init` | one-shots on every `up` | |
| `cli`, `backup` (profile `ops`) | one-off containers: `scanner <command>`, backups | `backup` alone mounts the Docker socket |

Docker publishes ports past ufw, so nothing else is published on a public interface; the UIs are on the loopback (see "Dagster UI and MinIO console"). On the server:

- `/etc/grid-scanner/env` (mode 600, owner `deploy`): every setting and secret ("Settings and secrets").
- `/opt/grid-scanner/releases/<sha>/`: the `infra/` files taken from the deployed image, so the compose file always matches the code; `current` links to the deployed one; `bin/` holds `deploy`, `compose`, `scanner` and `mc`, on the `deploy` user's PATH; `deployments.log` lists every deploy.
- `/var/lib/grid-scanner/exports` (static export) and `/var/lib/grid-scanner/backups` (dumps, `restore-checks.log`).

Memory, measured idle in the rehearsal: about 1.6 GB (Dagster webserver and daemon 0.56 GB each, with their three code-location processes; Postgres 0.3 GB, rising towards its 1 GB shared buffers; MinIO 0.1 GB; Caddy and PostgREST under 0.05 GB). Runs and the page cache use the rest; bootstrap adds 4 GB of swap.

Disk budget (80 GB, one disk for everything):

| What | Size | Notes |
|---|---|---|
| Ubuntu and Docker | about 5 GB | |
| Images | about 1 GB per app release (no Chromium), 5 releases kept; PostGIS 0.6 GB, the rest 0.4 GB | deploy prunes older releases and their images |
| Postgres | about 1 GB of data now, WAL up to 2 GB (`max_wal_size`) | the weekly restore check briefly adds one more copy |
| MinIO (raw captures) | the one that grows; see `mc du local/raw` | another agent is reducing raw growth |
| Backups | about 30 MB per dump today (the 2026-10-18 dump: 29.7 MB), `BACKUP_KEEP` 14 | |
| Container logs | at most 50 MB per container (json-file, 10 MB x 5) | |
| Dagster event logs | in the `dagster` database, about 150 runs a day | not pruned automatically |

The health alert fires at `DISK_ALERT_PERCENT` (85 %) of the disk, the nightly backup checks it too, and every deploy prints the usage.

### Settings and secrets

All in `/etc/grid-scanner/env` (template `infra/server-env.template`); compose reads it for interpolation, so each container gets only the names it needs, and nothing secret is in the image. Bootstrap fills in the generated ones.

| Name | Who sets it | What |
|---|---|---|
| `DOMAIN` | bootstrap: `<ip-with-dashes>.sslip.io`; you, once a DNS name points at the server | the HTTPS name; Let's Encrypt needs ports 80 and 443 open and the name resolving to the server. sslip.io resolves `<a-b-c-d>.sslip.io` to `a.b.c.d`, so it works without your own domain |
| `CORS_ALLOWED_ORIGINS` | you (optional) | the Lovable app's origins, comma-separated, e.g. `https://<app>.lovable.app,https://<your domain>`. Empty: any origin, which is safe for a public, read-only API without cookies; PostgREST then answers `*` |
| `POSTGRES_PASSWORD`, `AUTHENTICATOR_PASSWORD`, `MINIO_ROOT_PASSWORD`, `AWS_SECRET_ACCESS_KEY` | bootstrap (random hex) | database superuser; PostgREST's login role (deploy sets it on the role every time); MinIO admin; the scanner's MinIO key |
| `RAW_STORE_URL`, `FSSPEC_S3_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID`, `AWS_DEFAULT_REGION` | template: `s3://raw/grid-scanner` on `http://minio:9000`, key `scanner-raw` | the raw store; s3fs takes the endpoint from fsspec's environment config and the key from botocore's |
| `ENTSOE_API_TOKEN` | **you** | required by the ENTSO-E ingests (Transparency Platform account, "Web API Security Token") |
| `USER_AGENT` | **you** | put a contact address in it |
| `SCANNER_ALLOW_PROPOSED_ASSUMPTIONS` | template: `true` | needed while owner-approved assumption values are null, else the congestion score refuses to run; outputs say they used proposed values |
| `DATAFORDELER_API_KEY` | leave empty | only `scripts/build_geometries.py` needs it, to rebuild geometries where you rebuild them (the output goes to git) |
| `DAGSTER_UI_USER`, `DAGSTER_UI_HASH`, `DAGSTER_UI_HOST` | you | the Dagster site's basic auth: user (default `operator`), bcrypt hash in single quotes (empty: the site is locked), host name without `https://` (empty: `dagster.$DOMAIN`; alert links use `https://<host>`). "Dagster UI and MinIO console" |
| `ALERT_WEBHOOK_URL`, `ALERT_WEBHOOK_FORMAT`, `DISK_ALERT_PERCENT` | you (optional) | "Alerts" |
| `BACKUP_KEEP`, `BACKUP_OFFSITE_URL`, `BACKUP_OFFSITE_OPTIONS` | you (optional) | "Backups on the VPS" |
| `API_JWT_SECRET` | bootstrap (random hex, at least 32 characters; also added on a re-run to an env file that lacks it) | PostgREST verifies bearer tokens with it; the app containers use it for `scanner token`. "Raw data access" |
| `API_MAX_ROWS` | template: `50000` | most rows one API response holds; clients page with `limit` and `offset` |
| `ANALYTICS_DATABASE_URL` | you (optional) | "Analytics role" above, with `@db:5432` |

Outside the env file: a GitHub token to pull the private image (`docker login ghcr.io` as `deploy`; a classic personal access token with only `read:packages`, which is what GHCR accepts; or make the package public in its settings), `/etc/grid-scanner/rclone.conf` for an rclone off-site target, and, for deploys from GitHub Actions, the secret `VPS_SSH_KEY` and the variables `VPS_HOST`, `VPS_KNOWN_HOSTS` (and `VPS_USER`, default `deploy`). Rotate the ENTSO-E token and storage keys yearly or on any suspected leak; after changing a value in the env file, run `deploy <current sha>` so the containers pick it up.

### Raw data access

Six `serving` views with the stored series, entities, facts, feature values and all scenarios' scores are readable only with a bearer token (`docs/ui-contract.md`, "Raw data"); the other views stay public. Mint a token on the server with `scanner token --days N` (default 14; HS256, role `authenticated`, signed with `API_JWT_SECRET`) and give it to the user, who sends `Authorization: Bearer <token>`. Tokens are not stored anywhere and cannot be revoked one by one: to revoke all of them, change `API_JWT_SECRET` in the env file and run `deploy <current sha>`. A token put into browser code is visible to everyone who opens the page, so use short lifetimes for that.

### Provisioning

1. Hetzner Console: CX33, an EU location, Ubuntu 24.04, your SSH key. Optional and recommended: a Hetzner Cloud Firewall allowing inbound TCP 22, 80, 443 and UDP 443, a second layer Docker cannot bypass. (No cloud-init file: the repository is private, so the script is copied over instead of fetched.)
2. Optional: a DNS A record for your domain pointing at the server's IPv4.
3. From the repository root on the laptop:
   ```sh
   scp -r infra root@<ip>:/root/grid-scanner-infra
   ssh root@<ip> bash /root/grid-scanner-infra/bootstrap.sh
   ```
   It installs Docker Engine and the compose plugin from Docker's apt repository, ufw (SSH, 80 and 443 only), unattended security upgrades (a kernel update reboots at 04:30 UTC), 4 GB swap, the user `deploy` (root's SSH keys, group docker, no password), key-only SSH, the directories, the env file and the backup timers. Re-running it changes nothing that is in place and only adds names missing from the env file.
4. As `deploy` (`ssh deploy@<ip>`): edit `/etc/grid-scanner/env` (at least `ENTSOE_API_TOKEN`, `USER_AGENT`, and `DOMAIN` if you have a name), then `docker login ghcr.io -u <github user>`.

### First deploy

1. GitHub, Actions, workflow `image`, "Run workflow" on `main` (about 5 minutes). It pushes `ghcr.io/omincev/grid-feasibility-scanner:<sha>` and `:main`.
2. On the server: `deploy main`. It pulls the image, unpacks its `infra/` files as the release, starts `db`, runs the migrations and sets PostgREST's password in one-off containers, starts everything (minio-init creates the bucket and the scanner's key, limited to that bucket and without delete), runs `load-curated`, and checks: PostgREST `/zone_card` answers 200, all three Dagster code locations are loaded (GraphQL `workspaceOrError`), the newest raw captures read back from MinIO with the SHA-256 their sidecars record, HTTPS through Caddy answers 200, and disk usage. It ends with a summary and the rollback command; the exit code is 1 when a check failed.
3. `curl https://<DOMAIN>/zone_card` answers `[]` until the initial load is done.

### Initial data load on an empty server

`deploy` has already migrated and loaded the curated files. Then, in `tmux` (it runs for hours), as `deploy`:

```sh
scanner capacity-map                                          # stations, DSO areas, capacity facts
scanner snapshot                                              # every active snapshot
scanner ingest --dataset all --from 2025-03-04 --workers 3    # Energi Data Service and ENTSO-E history
scanner pipeline --all-scenarios                              # features, scores, valuation, export
```

`--workers 3` runs three datasets at a time; days of one dataset stay in order. The ENTSO-E dossier estimates 15 to 20 minutes each for prices and generation (`docs/sources/eu/entsoe.md`, "Backfill"; its limit of 400 requests a minute per token stays clear at 2 requests a second per dataset); the retired Elspotprices takes about 35 minutes (`docs/sources/dk/energidataservice.md`). Failed days are listed at the end; rerun the same command with `--from` and `--to` for them. Meanwhile the schedules keep today current, and `job_compute` recomputes everything at 15:30 Copenhagen time each day. Check with `curl "https://<DOMAIN>/source_completeness?select=dataset,partitions_missing,partitions_incomplete"`.

### Alternatively: restore a dump from the laptop

The database comes from a dump; the raw captures are files, not in the dump, and go into MinIO with the same relative paths (references are `raw://...` relative to `RAW_STORE_URL`, so nothing in the database changes).

```sh
# laptop, repository root (Git Bash): a dump, and the raw store as files
uv run python -m scanner.cli raw-uris migrate          # must report 0 to rewrite; else run it with --yes first
uv run python -m scanner.cli backup --dir .data/to-vps --keep 1
scp .data/to-vps/scanner-<stamp>.dump deploy@<ip>:/var/lib/grid-scanner/backups/
scp -r .data/raw deploy@<ip>:import-raw                # or rsync -a from WSL

# server, as deploy, after the first deploy
compose stop dagster-daemon dagster-webserver postgrest
compose run --rm backup restore /backups/scanner-<stamp>.dump --database scanner_restored
compose exec db psql -U scanner -d postgres -c "alter database scanner rename to scanner_empty" \
                                            -c "alter database scanner_restored rename to scanner"
MC_IMPORT=$HOME/import-raw mc mirror /import local/raw/grid-scanner
deploy main                                            # newer migrations, load-curated, everything up, checks
scanner raw-uris migrate --verify                      # every reference resolves in MinIO (writes nothing)
scanner ingest --dataset all --from <day of the dump> --workers 3   # the gap
scanner pipeline --all-scenarios
compose exec db dropdb -U scanner scanner_empty
rm -r ~/import-raw
```

The roles are cluster-wide and not in the dump; the server's migrations created them on the first deploy, so the dump's grants apply.

### Daily operation

- Dagster schedules (location `ingestion`; "Orchestrator start-up" above): the ingests, yesterday's re-ingest at 01:30, snapshots, the Monday capacity map; `schedule_compute_daily` at 15:30 (location `analytics`).
- Backups: 02:30 UTC daily; restore check Sundays 03:15 UTC (`systemctl list-timers 'grid-scanner*'`, `journalctl -u grid-scanner-backup`, `/var/lib/grid-scanner/backups/restore-checks.log`).
- Alerts, when `ALERT_WEBHOOK_URL` is set: failed runs, stale sources and recent gaps, disk usage, failed backups and restore checks.
- Ubuntu security updates daily, with a reboot at 04:30 UTC when a kernel update needs one. Containers come back on their own (`restart: unless-stopped`); run monitoring (`infra/dagster.yaml`) fails a run the reboot cut off after at most 6 hours, which also releases its source's slot in the run queue.
- Inspect: `compose ps`, `compose logs -f dagster-daemon`, `scanner quarantine list`, `mc du local/raw`.

### Dagster UI and MinIO console

Dagster is also public at `https://dagster.$DOMAIN/` (sslip.io resolves `dagster.<ip-with-dashes>.sslip.io` to the server; with your own `DOMAIN` add a DNS A record for `dagster.<domain>`, or set `DAGSTER_UI_HOST`), behind Caddy basic auth. It is the full UI. Dagster has no users or permissions, so the password is the only guard on launching, wiping and deleting runs and materializations: use a long random one. Caddy does not rate-limit logins. Turn it on:

```sh
docker run -it --rm caddy:2.10.2-alpine caddy hash-password   # prompts twice; prints $2a$14$...
# in /etc/grid-scanner/env, the hash in SINGLE quotes (it contains $):
#   DAGSTER_UI_HASH='$2a$14$...'
deploy <current sha>   # recreates Caddy with the new values; the smoke check expects HTTP 401 without credentials
```

With an empty `DAGSTER_UI_HASH` compose substitutes a hash of a random password nobody knows, so the site answers 401 to everyone and Caddy (and the public API) still starts. The deploy's smoke check fails if the site answers 200 without credentials.

The loopback bindings stay. SSH tunnel, nothing to install:

```sh
ssh -N -L 3000:127.0.0.1:3000 -L 9001:127.0.0.1:9001 deploy@<ip>
# then http://localhost:3000 (Dagster) and http://localhost:9001 (MinIO: MINIO_ROOT_USER / MINIO_ROOT_PASSWORD)
```

Tailscale, for a URL that works without a tunnel, only for members of your tailnet:

```sh
curl -fsSL https://tailscale.com/install.sh | sudo sh
sudo tailscale up
sudo tailscale serve --bg --https=8443 http://127.0.0.1:3000   # https://<machine>.<tailnet>.ts.net:8443
sudo ufw allow in on tailscale0                                # if the page does not load
```

Keep Funnel off, and never publish 3000 or 9001 on `0.0.0.0` in the compose file.

### Upgrades

- Code: run the `image` workflow for the commit (after `ci` is green), then `deploy <sha>`, or tick "deploy" in the workflow once SSH deploys are set up. `deploy` waits up to 15 minutes (`GS_DRAIN_TIMEOUT`) for running Dagster runs, and marks runs it still cuts off as canceled. A changed `infra/` (compose file, Caddyfile, scripts) comes with the image; changed systemd units in `infra/systemd/` need `bootstrap.sh` again.
- Pinned images (PostGIS, PostgREST, Caddy, MinIO and its client): change the digest in `infra/docker-compose.prod.yml` in a pull request, build, deploy. MinIO no longer publishes images; the stack uses Chainguard's build of MinIO from source, whose free tier only has `latest` and `latest-dev` tags, so an old digest can disappear: re-pin with `docker pull cgr.dev/chainguard/minio:latest-dev` and `docker inspect --format '{{index .RepoDigests 0}}' cgr.dev/chainguard/minio:latest-dev` (same for `minio-client`).
- Docker Engine and other packages outside the security origin: `sudo apt update && sudo apt upgrade` now and then.

### Rollback

`deploy <previous sha>`: each deploy prints it, and `/opt/grid-scanner/deployments.log` lists them all. The images of the last five releases stay on the server. Migrations are forward-only: rolled-back code runs on the newer schema (migrations add; they never edit an applied one). If a migration damaged data, restore the last dump.

### Restore

- Database on this server: as in "Alternatively: restore a dump from the laptop", from a dump in `/var/lib/grid-scanner/backups` (stop, restore into `scanner_restored`, rename, `deploy`), then backfill the days since the dump.
- Server lost: a new server, bootstrap, deploy, the off-site dump into `/var/lib/grid-scanner/backups`, the same steps. The raw captures exist only on this server's disk until they move to R2 (below) or are mirrored off-site (`mc mirror local/raw/grid-scanner <alias>/<bucket>/grid-scanner`); without them, re-ingest.
- Not in the dump: the raw store (MinIO) and the `dagster` database (run history and schedule toggles, which can be rebuilt).

### Moving the raw store to Cloudflare R2

1. In Cloudflare: an R2 bucket and an API token with object read and write on it.
2. Copy what is there (incremental, safe to repeat; leading space keeps the secret out of the shell history):
   ```sh
    MC_HOST_r2=https://<key id>:<secret>@<account id>.r2.cloudflarestorage.com mc mirror local/raw/grid-scanner r2/<bucket>/grid-scanner
   ```
3. In the env file: `RAW_STORE_URL=s3://<bucket>/grid-scanner`, `FSSPEC_S3_ENDPOINT_URL=https://<account id>.r2.cloudflarestorage.com`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` from the token, `AWS_DEFAULT_REGION=auto`.
4. `deploy <current sha>`: the containers get the new settings, minio-init does nothing (the endpoint is not the local MinIO), and the smoke check reads the newest captures back from R2. Run the mirror once more for captures written in between.

The database does not change: references are relative. MinIO can stay idle afterwards.

### Backups on the VPS

A systemd timer, not a Dagster schedule: `scanner.cli backup` runs `pg_dump` in the db container through `docker exec`, so it needs the Docker socket, which a one-off container may hold for a minute but no long-running service should; and backups must keep working when Dagster is down or being redeployed.

- `grid-scanner-backup.timer` (02:30 UTC daily) runs `infra/backup.sh`: `scanner.cli backup` in the `backup` container (dump, checked with `pg_restore --list`, newest `BACKUP_KEEP` kept, default 14), then the off-site copy, then the disk check.
- Off-site copy when `BACKUP_OFFSITE_URL` is set: `rclone:<remote>:<path>` uses the host's rclone with `/etc/grid-scanner/rclone.conf` (for example a Hetzner Storage Box over SFTP: `rclone config --config /etc/grid-scanner/rclone.conf`); any other URL is an fsspec directory URL, copied by `scripts/offsite_copy.py` with the storage options in `BACKUP_OFFSITE_OPTIONS`, e.g. for R2 `BACKUP_OFFSITE_URL=s3://<backup bucket>/grid-scanner` and `BACKUP_OFFSITE_OPTIONS='{"endpoint_url": "https://<account id>.r2.cloudflarestorage.com", "key": "...", "secret": "..."}'`. Expire old copies with the bucket's lifecycle rule.
- `grid-scanner-restore-check.timer` (Sundays 03:15 UTC) runs `infra/restore-check.sh`: the newest dump is restored into `scanner_restore_check`, row counts of the main tables are compared with the live database (a table with rows that restores empty fails the check), the scratch database is dropped, and the result is appended to `restore-checks.log`.
- By hand: `sudo systemctl start grid-scanner-backup` (or `/opt/grid-scanner/current/backup.sh` as `deploy`).

### Alerts

Off unless `ALERT_WEBHOOK_URL` is set. The code location `ops` (`scanner.ops_definitions`, plain functions in `scanner.runtime.alerts`) imports neither ingestion nor analytics modules, so alerts keep running when either fails to load:

- `sensor_run_failure_alert`: every failed run in any code location, with the error and the run's link (`DAGSTER_UI_URL`: on the VPS `https://<DAGSTER_UI_HOST>`, set by compose; elsewhere default `http://localhost:3000`).
- `sensor_health_alert`, every 15 minutes: active datasets that are stale (`serving.source_freshness`), missing or incomplete days in the last 7 days (`serving.source_completeness`, `ALERT_GAP_DAYS`), and disk usage at or above `DISK_ALERT_PERCENT`. Each problem is sent once when it appears and once when it is resolved.
- `backup.sh` and `restore-check.sh` post their own failures, and the disk usage once a night while it is above the threshold.

Both sensors start as running only where the webhook is set and schedules run by default (the VPS); elsewhere they load stopped. Formats: plain text with a `Title` header (ntfy and most receivers), Slack JSON for `hooks.slack.com` URLs (or `ALERT_WEBHOOK_FORMAT=slack`), or Discord JSON for `discord.com/api/webhooks/` URLs (or `ALERT_WEBHOOK_FORMAT=discord`; cut to 2000 characters, mentions off). Any other receiver that wants JSON answers HTTP 400, which fails the sensor's tick. With ntfy, `ALERT_WEBHOOK_URL=https://ntfy.sh/<long random topic>`: anyone who knows the topic can read it. Test: `curl -H "Title: test" -d "hello" "<url>"`.

### Deploys from GitHub Actions (optional)

The `image` workflow is manual only (`workflow_dispatch`, input `ref`, default `main`); its `deploy` job runs only when "deploy" is ticked and does nothing but print a notice until the secret `VPS_SSH_KEY` exists. To switch it on: make a key pair for CI only, add its public key to `~deploy/.ssh/authorized_keys` as the line `restrict,command="/usr/local/bin/grid-scanner-ci-deploy" ssh-ed25519 AAAA... grid-scanner-ci` (the `deploy` user is in the docker group, which is root-equivalent, so the key may only deploy a commit SHA whose image is already in GHCR; on an existing server re-run `bootstrap.sh` to install the wrapper), store the private key as the repository secret `VPS_SSH_KEY`, and set the repository variables `VPS_HOST` and `VPS_KNOWN_HOSTS` (the output of `ssh-keyscan -t ed25519 <ip>`, checked against the server's own `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`).

### CI

`ci` runs on pull requests and pushes to `main`, except docs-only changes: `docs/**` and `**/*.md` are skipped, but `docs/methods/**` and `docs/sources/**` still run it, because the curated loader, the contract checks and the asset metadata read them. A newer push to the same pull request or branch cancels the run it supersedes. If branch protection ever requires `ci`, a docs-only pull request waits for a check that never runs. The image is built only by the manual `image` workflow.

### Runbook notes

- Restore tests are recorded here with date and result. 2026-10-18 (laptop): a copy of `scanner` (3,046,112 observations, 4,257 ingest runs) dumped with `backup` (29.7 MB, 372 TOC entries) and restored with `restore` into a scratch database: row counts of `core.observation`, `ops.ingest_run`, `core.series`, `feat.feature_value` and `ref.entity` equal. On the VPS the weekly check writes `restore-checks.log`.
- Degraded source: the front end shows the last good data with a staleness label (`serving.source_freshness`); the health alert names it; the outbox bead is the work item.
- New migration: try it on a copy first (restore the newest dump into a scratch database, migrate it with `scripts/migrate_local.py`, run `pytest -m db` against it), then deploy.
- Factory (Phase 4): a separate machine that never shares credentials with production.

### Rehearsal (2026-10-18)

The whole procedure ran locally on a simulated fresh server: a Docker-in-Docker container (`docker:28.5.1-dind`) with its own Docker daemon and volumes, ports 80 and 443 published on the laptop as 127.0.0.1:18080 and 18443, so the main stack's containers, volumes and database were never touched. In it: `bootstrap.sh --files-only` (the apt, ufw, swap, user and systemd parts need a real Ubuntu server); the image built without Chromium (971 MB) and loaded instead of pulled (`GS_PULL=0`); `deploy` (63 s, every check ok, Caddy with its internal CA for `DOMAIN=localhost`, `GS_INSECURE_TLS=1`); the initial load with a three-day window (capacity map, 53 snapshots, Energi Data Service ingests ok, ENTSO-E failing cleanly for want of a token, which the rehearsal did not have, then `pipeline --all-scenarios`, every step ok); raw captures written to MinIO by the ingests and read back through `RawStore.get` with matching SHA-256 (`deploy_checks.py raw`); the scanner's MinIO key refused a delete; `curl -k https://localhost:18443/zone_card` from the laptop served DK1 and DK2, POST and DELETE got 405, the CORS list was honoured; `backup.sh` with an fsspec off-site copy to a second bucket, and `restore-check.sh` (equal counts); run-failure and health alerts arrived at a test webhook; an upgrade deploy, a rollback while a compute run was in progress (the run was marked canceled), and a deploy back. Findings fixed on the way: Caddy sorted `handle` before `respond` (writes reached PostgREST), and minio-init's attach check matched the key's own name. Then everything was removed.
