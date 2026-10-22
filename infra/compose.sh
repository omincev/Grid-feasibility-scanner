#!/usr/bin/env bash
# docker compose for the deployed stack (infra/README.md, "VPS"): compose ps | logs -f dagster-daemon | ...
# Installed as /opt/grid-scanner/bin/compose by deploy.sh.
set -euo pipefail
here=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
# shellcheck source=infra/lib.sh
. "$here/lib.sh"
compose "$@"
