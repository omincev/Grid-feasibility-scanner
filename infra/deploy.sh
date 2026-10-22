#!/usr/bin/env bash
# Deploy one image of the Grid Opportunity Scanner on the VPS (infra/README.md, "VPS"):
#   /opt/grid-scanner/bin/deploy <git sha|main>
#
#  1. pull ghcr.io/omincev/grid-feasibility-scanner:<ref>; its infra/ files become the release (compose file, Caddyfile,
#     these scripts), so the compose file always matches the code; `main` is recorded by its commit SHA
#  2. wait (bounded) for in-progress Dagster runs, so a deploy rarely cuts one off
#  3. database first, then migrations and PostgREST's login password as one-off containers
#  4. everything up (the MinIO bucket and key by minio-init); runs cut off by replaced containers are marked
#     canceled, so they do not hold their source's slot in the run queue
#  5. load-curated
#  6. smoke check: PostgREST /zone_card 200, every Dagster code location loaded, raw captures read back from
#     the raw store, HTTPS through Caddy, disk usage
#  7. summary. Rollback: deploy the previous SHA (printed below, and in /opt/grid-scanner/deployments.log).
#
# Runs as the deploy user (docker group). Rehearsal overrides: GS_ROOT, ENV_FILE, GS_PULL=0 (image loaded
# locally), GS_INSECURE_TLS=1 (Caddy's internal CA for DOMAIN=localhost), GS_DRAIN_TIMEOUT.
set -euo pipefail
here=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
# shellcheck source=infra/lib.sh
. "$here/lib.sh"

[ $# -eq 1 ] || die "usage: deploy.sh <git sha|main>"
ref=$1
[[ $ref =~ ^[A-Za-z0-9._-]+$ ]] || die "not an image tag: $ref"
[ -r "$ENV_FILE" ] || die "cannot read $ENV_FILE (run infra/bootstrap.sh as root first)"
# Compose expands `$` in unquoted (and double-quoted) env-file values, and its warning prints the would-be variable
# name, a fragment of the secret: a value containing `$` (a bcrypt hash, a password) must be in single quotes.
unquoted=$(grep -nE "^[A-Za-z_][A-Za-z0-9_]*=[^']*[\$]" "$ENV_FILE" | cut -d= -f1 | paste -sd' ' -) || true
[ -z "$unquoted" ] || die "single-quote the values of these lines in $ENV_FILE, they contain \$: $unquoted"
image=$(env_value IMAGE)
image=${image:-ghcr.io/omincev/grid-feasibility-scanner}
domain=$(env_value DOMAIN)
[ -n "$domain" ] || die "set DOMAIN in $ENV_FILE"
case $(env_value DAGSTER_UI_HOST) in
*/* | *:*) die "DAGSTER_UI_HOST is a host name without https:// (empty: dagster.$domain)" ;;
esac
https_port=$(env_value HTTPS_PORT)
https_port=${https_port:-443}
started=$(date +%s)
mkdir -p "$GS_ROOT/releases" "$GS_ROOT/bin"
exec 9>"$GS_ROOT/.deploy.lock"
flock -n 9 || die "another deploy is running"

# 1. Image and release files
if [ "${GS_PULL:-1}" = 1 ]; then
	log "pulling $image:$ref"
	docker pull -q "$image:$ref" >/dev/null
fi
sha=$(docker image inspect -f '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image:$ref") ||
	die "no image $image:$ref"
[[ $sha =~ ^[0-9a-f]{7,40}$ ]] || die "image $image:$ref has no commit SHA label (got '$sha')"
[ "$ref" = "$sha" ] || docker tag "$image:$ref" "$image:$sha"
release=$GS_ROOT/releases/$sha
tmp=$(mktemp -d "$GS_ROOT/releases/.incoming.XXXXXX")
cid=$(docker create "$image:$sha")
docker cp "$cid:/app/infra/." "$tmp/" >/dev/null
docker rm "$cid" >/dev/null
chmod 755 "$tmp" "$tmp"/*.sh # mktemp makes the directory private
printf '%s\n' "$sha" >"$tmp/TAG"
rm -rf "$release"
mv "$tmp" "$release"
previous=$(basename "$(readlink "$GS_ROOT/current" 2>/dev/null || echo none)")
export RELEASE_DIR=$release TAG=$sha
log "release $sha from $image:$ref (previous: $previous)"

checks() { compose run --rm --no-deps -T --entrypoint python cli scripts/deploy_checks.py "$@"; }

# 2. Let running Dagster runs finish
if compose ps --status running --services | grep -qx dagster-webserver; then
	log "waiting for in-progress Dagster runs (at most ${GS_DRAIN_TIMEOUT:-900} s)"
	checks drain --timeout "${GS_DRAIN_TIMEOUT:-900}" || log "runs still in progress; continuing"
fi

# 3. Database, migrations, PostgREST's login password (roles are cluster-wide and not in the migrations' data)
compose up -d --wait db
log "migrations"
compose run --rm --no-deps -T cli migrate
# shellcheck disable=SC2016 # $DATABASE_URL expands in the container
compose run --rm --no-deps -T --entrypoint sh cli -c 'python scripts/set_authenticator_password.py "$DATABASE_URL"'

# 4. Everything up
before=$(compose ps -q dagster-daemon dagster-webserver | sort | tr '\n' ' ')
cutoff=$(date +%s)
log "starting the stack"
compose up -d --remove-orphans --wait --wait-timeout 600
ln -sfn "releases/$sha" "$GS_ROOT/current"
for f in deploy compose scanner mc; do ln -sfn "../current/$f.sh" "$GS_ROOT/bin/$f"; done
rm -f "$GS_ROOT/bin/lib.sh" # bootstrap's copy; the linked scripts source the release's own
after=$(compose ps -q dagster-daemon dagster-webserver | sort | tr '\n' ' ')
if [ -n "${before// /}" ] && [ "$before" != "$after" ]; then
	checks cancel-orphans --started-before "$cutoff"
fi
# New serving views from the migrations: PostgREST reloads its schema cache on this notification.
compose exec -T db psql -U scanner -d scanner -qc "notify pgrst, 'reload schema'"

# 5. Curated files (registry, codebook, assumptions, method cards) from this image
log "load-curated"
compose run --rm --no-deps -T cli load-curated | tail -n 15

# 6. Smoke check
smoke=ok
log "smoke check"
checks smoke || smoke=FAILED
code=000
for _ in 1 2 3 4 5 6 7 8 9; do # a first certificate can take a few seconds
	code=$(curl -sS -o /dev/null -w '%{http_code}' -m 20 ${GS_INSECURE_TLS:+--insecure} \
		--resolve "$domain:$https_port:127.0.0.1" "https://$domain:$https_port/zone_card?limit=1" 2>/dev/null) || true
	[ "$code" = 200 ] && break
	sleep 10
done
if [ "$code" = 200 ]; then
	echo "ok   https://$domain/zone_card through Caddy: HTTP 200"
else
	echo "FAIL https://$domain/zone_card through Caddy: HTTP $code (DNS for $domain, ports 80/443, compose logs caddy)"
	smoke=FAILED
fi
# The Dagster UI must refuse requests without credentials; 200 means it is public without auth.
dagster_host=$(env_value DAGSTER_UI_HOST)
dagster_host=${dagster_host:-dagster.$domain}
code=000
for _ in 1 2 3 4 5 6 7 8 9; do
	code=$(curl -sS -o /dev/null -w '%{http_code}' -m 20 ${GS_INSECURE_TLS:+--insecure} \
		--resolve "$dagster_host:$https_port:127.0.0.1" "https://$dagster_host:$https_port/" 2>/dev/null) || true
	[ "$code" = 401 ] && break
	[ "$code" = 200 ] && break
	sleep 10
done
if [ "$code" = 401 ]; then
	echo "ok   https://$dagster_host/ without credentials: HTTP 401"
elif [ "$code" = 200 ]; then
	echo "FAIL https://$dagster_host/ answered 200 WITHOUT credentials: Dagster is public without auth (compose logs caddy)"
	smoke=FAILED
else
	echo "FAIL https://$dagster_host/ without credentials: HTTP $code, expected 401 (DNS record for $dagster_host, compose logs caddy)"
	smoke=FAILED
fi
dir=$(data_dir)
read -r pcent avail size < <(df -h --output=pcent,avail,size "$dir" | tail -n 1)
limit=$(env_value DISK_ALERT_PERCENT)
limit=${limit:-85}
disk_note=ok
[ "${pcent%\%}" -lt "$limit" ] || disk_note="WARN (alert threshold $limit%)"
echo "disk $disk_note: $pcent used of $size, $avail free on the filesystem of $dir;" \
	"docker: $(docker system df --format '{{.Type}} {{.Size}}' | paste -sd, - | sed 's/,/, /g')"

# 7. Record, prune old releases and images (the newest five stay for rollback), summary
printf '%s %s %s smoke=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$sha" "$ref" "$smoke" >>"$GS_ROOT/deployments.log"
# shellcheck disable=SC2012 # release directories are named by commit SHA
mapfile -t old < <(cd "$GS_ROOT/releases" && ls -1t | tail -n +6)
for r in "${old[@]}"; do [ "$r" = "$previous" ] || rm -rf "${GS_ROOT:?}/releases/$r"; done
while read -r tag; do
	[ "$tag" = main ] || [ -d "$GS_ROOT/releases/$tag" ] || docker rmi -f "$image:$tag" >/dev/null || true
done < <(docker images "$image" --format '{{.Tag}}')
docker image prune -f >/dev/null

echo
echo "== deployed $sha ($ref) in $(($(date +%s) - started)) s; smoke check: $smoke"
compose ps --format 'table {{.Service}}\t{{.Status}}'
echo "API:      https://$domain/  (static export: https://$domain/exports/)"
echo "Dagster:  ssh -N -L 3000:127.0.0.1:3000 <user>@<server>, then http://localhost:3000"
[ "$previous" = none ] || [ "$previous" = "$sha" ] || echo "rollback: $GS_ROOT/bin/deploy $previous"
[ "$smoke" = ok ]
