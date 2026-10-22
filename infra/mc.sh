#!/usr/bin/env bash
# mc (the MinIO client) as the MinIO admin, on the compose network (infra/README.md, "VPS"):
#   mc du local/raw          mc ls --recursive local/raw/grid-scanner/raw/energidataservice | head
# MC_IMPORT=<host directory> mounts it read-only at /import (copying the laptop's raw store in):
#   MC_IMPORT=$HOME/import-raw mc mirror /import local/raw/grid-scanner
# Other S3 endpoints are aliases given as MC_HOST_<alias> variables, e.g. Cloudflare R2:
#   MC_HOST_r2=https://<key>:<secret>@<account>.r2.cloudflarestorage.com mc mirror local/raw/grid-scanner r2/<bucket>/grid-scanner
# Installed as /opt/grid-scanner/bin/mc by deploy.sh.
set -euo pipefail
here=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
# shellcheck source=infra/lib.sh
. "$here/lib.sh"
args=(--rm)
[ -t 0 ] && [ -t 1 ] || args+=(-T)
[ -z "${MC_IMPORT:-}" ] || args+=(-v "$MC_IMPORT:/import:ro")
while read -r name; do args+=(-e "$name"); done < <(compgen -e | grep '^MC_HOST_' || true)
# shellcheck disable=SC2016 # expands in the container, where the root credentials are
compose run "${args[@]}" --entrypoint /bin/sh minio-init -c \
	'export MC_HOST_local="http://$MINIO_ROOT_USER:$MINIO_ROOT_PASSWORD@minio:9000"; exec mc "$@"' mc "$@"
