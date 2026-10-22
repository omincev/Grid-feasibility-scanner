#!/usr/bin/env bash
# A scanner CLI command in a one-off container of the deployed image (python -m scanner.cli <command> ...):
#   scanner ingest --dataset all --from 2025-03-04 --workers 4
# Installed as /opt/grid-scanner/bin/scanner by deploy.sh (infra/README.md, "VPS").
set -euo pipefail
here=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
# shellcheck source=infra/lib.sh
. "$here/lib.sh"
tty=()
[ -t 0 ] && [ -t 1 ] || tty=(-T)
compose run --rm "${tty[@]}" cli "$@"
