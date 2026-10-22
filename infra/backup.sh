#!/usr/bin/env bash
# Daily database backup on the VPS (systemd grid-scanner-backup.timer; infra/README.md, "Backups on the VPS"):
#  1. `scanner.cli backup` in the one-off `backup` container: pg_dump -Fc in the db container, checked with
#     pg_restore --list, written to $DATA_DIR/backups, keeping the newest BACKUP_KEEP (default 14) dumps;
#  2. an off-site copy of the new dump when BACKUP_OFFSITE_URL is set: `rclone:<remote>:<path>` with the host's
#     rclone (config /etc/grid-scanner/rclone.conf), any other URL with fsspec (scripts/offsite_copy.py);
#  3. a disk-usage check against DISK_ALERT_PERCENT.
# Any failure posts to ALERT_WEBHOOK_URL (when set) and fails the unit.
set -euo pipefail
here=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
# shellcheck source=infra/lib.sh
. "$here/lib.sh"
trap 'alert "Grid Opportunity Scanner: backup failed" "backup.sh failed on $(hostname) (line $LINENO); journalctl -u grid-scanner-backup"' ERR

keep=$(env_value BACKUP_KEEP)
out=$(compose run --rm --no-deps -T backup backup --keep "${keep:-14}")
printf '%s\n' "$out"
dump=$(basename "$(printf '%s\n' "$out" | head -n 1 | cut -d' ' -f1)")
[[ $dump == *.dump ]] || {
	log "unexpected backup output"
	false
}

offsite=$(env_value BACKUP_OFFSITE_URL)
case "$offsite" in
"") ;;
rclone:*)
	rclone --config /etc/grid-scanner/rclone.conf copyto "$(data_dir)/backups/$dump" "${offsite#rclone:}/$dump"
	log "copied $dump to ${offsite#rclone:}"
	;;
*) compose run --rm --no-deps -T --entrypoint python backup scripts/offsite_copy.py "/backups/$dump" ;;
esac

limit=$(env_value DISK_ALERT_PERCENT)
pcent=$(df --output=pcent "$(data_dir)" | tail -n 1 | tr -dc 0-9)
log "disk ${pcent}% used"
if [ "$pcent" -ge "${limit:-85}" ]; then
	alert "Grid Opportunity Scanner: disk ${pcent}% full" "$(hostname): $(df -h "$(data_dir)" | tail -n 1)"
fi
