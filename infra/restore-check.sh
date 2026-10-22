#!/usr/bin/env bash
# Weekly restore check (systemd grid-scanner-restore-check.timer; infra/README.md, "Backups on the VPS"): the
# newest dump is restored with `scanner.cli restore` into the scratch database scanner_restore_check, row counts
# of the main tables are compared with the live database, and the scratch database is dropped. The result is
# appended to $DATA_DIR/backups/restore-checks.log; a failure posts to ALERT_WEBHOOK_URL (when set).
#
# The live database moves on after the dump, so counts are not expected to be equal: the check fails when the
# restore fails or when a table that has rows in the live database restores empty.
set -euo pipefail
here=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
# shellcheck source=infra/lib.sh
. "$here/lib.sh"
trap 'alert "Grid Opportunity Scanner: restore check failed" "restore-check.sh failed on $(hostname) (line $LINENO); journalctl -u grid-scanner-restore-check"' ERR

scratch=scanner_restore_check
backups=$(data_dir)/backups
dump=$(find "$backups" -maxdepth 1 -name 'scanner-*.dump' -printf '%f\n' | sort | tail -n 1)
[ -n "$dump" ] || {
	log "no dump in $backups"
	false
}
sql() { compose exec -T -e PGOPTIONS='-c client_min_messages=warning' db psql -U scanner -d "$1" -tAc "$2"; }

sql postgres "drop database if exists $scratch" >/dev/null # left over from an interrupted check
log "restoring $dump into $scratch"
compose run --rm --no-deps -T backup restore "/backups/$dump" --database "$scratch"

result=ok
summary=""
for table in core.observation core.series ops.ingest_run feat.feature_value ref.entity; do
	live=$(sql scanner "select count(*) from $table")
	restored=$(sql "$scratch" "select count(*) from $table")
	summary+=" $table=$restored/$live"
	if [ "$live" -gt 0 ] && [ "$restored" -eq 0 ]; then result=FAILED; fi
done
sql postgres "drop database $scratch" >/dev/null
line="$(date -u +%Y-%m-%dT%H:%M:%SZ) $dump $result (restored/live)$summary"
printf '%s\n' "$line" | tee -a "$backups/restore-checks.log"
if [ "$result" != ok ]; then
	alert "Grid Opportunity Scanner: restore check failed" "$(hostname): $line"
	exit 1
fi
