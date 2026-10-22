#!/usr/bin/env bash
# SSH forced command for the CI deploy key (infra/README.md, "Deploys from GitHub Actions"). Installed by
# bootstrap.sh at /usr/local/bin/grid-scanner-ci-deploy, root-owned, and named in the key's authorized_keys line.
# Accepts only "deploy <sha>" or "/opt/grid-scanner/bin/deploy <sha>" with a full 40-character lowercase commit SHA.
set -euo pipefail
GS_ROOT=${GS_ROOT:-/opt/grid-scanner}
cmd=${SSH_ORIGINAL_COMMAND:-}

if [[ "$cmd" =~ ^(deploy|/opt/grid-scanner/bin/deploy)\ ([0-9a-f]{40})$ ]]; then
	sha=${BASH_REMATCH[2]}
	logger -t grid-scanner-ci-deploy "accepted: deploy $sha" || true
	exec "$GS_ROOT/bin/deploy" "$sha"
fi
logger -t grid-scanner-ci-deploy "rejected: ${cmd:0:200}" || true
echo "grid-scanner-ci-deploy: only 'deploy <40-character commit sha>' is allowed" >&2
exit 1
