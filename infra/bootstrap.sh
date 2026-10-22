#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu LTS server (24.04) for the Grid Opportunity Scanner (infra/README.md, "VPS").
# Idempotent: re-running it changes nothing that is already in place and never overwrites the env file.
#
# Run as root from a copy of the repository's infra/ directory:
#   scp -r infra root@<server>:/root/grid-scanner-infra
#   ssh root@<server> bash /root/grid-scanner-infra/bootstrap.sh
#
# Installs Docker Engine and the compose plugin (Docker's apt repository), ufw (SSH, 80 and 443 only), unattended
# security upgrades, a 4 GB swap file, the deploy user (root's SSH keys, docker group, no password), SSH with
# keys only, the directories, /etc/grid-scanner/env (mode 600, generated secrets), the deploy script and the
# backup and restore-check timers.
#   --files-only   only directories, env file, deploy script and timers (the rehearsal's simulated server)
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DEPLOY_USER=${DEPLOY_USER:-deploy}
GS_ROOT=${GS_ROOT:-/opt/grid-scanner}
ENV_FILE=${ENV_FILE:-/etc/grid-scanner/env}
DATA_DIR=${DATA_DIR:-/var/lib/grid-scanner}

log() { printf '== %s\n' "$*"; }

packages() {
	log "packages, Docker Engine and the compose plugin"
	export DEBIAN_FRONTEND=noninteractive
	apt-get update -q
	apt-get install -y -q ca-certificates curl openssl python3 rclone ufw unattended-upgrades
	install -m 0755 -d /etc/apt/keyrings
	if [ ! -s /etc/apt/keyrings/docker.asc ]; then
		curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
		chmod a+r /etc/apt/keyrings/docker.asc
	fi
	# shellcheck source=/dev/null
	. /etc/os-release
	echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc]" \
		"https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" \
		>/etc/apt/sources.list.d/docker.list
	apt-get update -q
	apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
	systemctl enable --now docker
}

upgrades() {
	log "unattended security upgrades"
	cat >/etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
	# Ubuntu's default origins are the security updates. A kernel update needs a reboot: at 04:30 server time
	# (UTC on Hetzner's images), after the backup and the restore check. Containers come back on their own, and
	# run monitoring (infra/dagster.yaml) fails runs the reboot cut off.
	cat >/etc/apt/apt.conf.d/52grid-scanner-upgrades <<'EOF'
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:30";
EOF
	systemctl enable --now unattended-upgrades
}

swap() {
	# 8 GB is shared by Postgres, MinIO and Dagster runs; swap absorbs a burst instead of the OOM killer.
	if ! swapon --show --noheadings | grep -q .; then
		log "4 GB swap file"
		fallocate -l 4G /swapfile
		chmod 600 /swapfile
		mkswap /swapfile >/dev/null
		swapon /swapfile
	fi
	grep -q '^/swapfile ' /etc/fstab || ! [ -f /swapfile ] || echo '/swapfile none swap sw 0 0' >>/etc/fstab
	echo 'vm.swappiness=10' >/etc/sysctl.d/60-grid-scanner.conf
	sysctl -q -p /etc/sysctl.d/60-grid-scanner.conf
}

firewall() {
	# Docker publishes ports past ufw: the compose file publishes only 80 and 443 publicly, the UIs on 127.0.0.1.
	log "firewall: SSH, 80 and 443"
	ufw default deny incoming >/dev/null
	ufw default allow outgoing >/dev/null
	ufw allow OpenSSH >/dev/null
	ufw allow 80/tcp >/dev/null
	ufw allow 443 >/dev/null # TCP for HTTPS, UDP for HTTP/3
	ufw --force enable >/dev/null
}

deploy_user() {
	log "user $DEPLOY_USER"
	id "$DEPLOY_USER" >/dev/null 2>&1 || adduser --disabled-password --gecos "" "$DEPLOY_USER" >/dev/null
	usermod -aG docker "$DEPLOY_USER"
	local home
	home=$(getent passwd "$DEPLOY_USER" | cut -d: -f6)
	install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$home/.ssh"
	if [ ! -s "$home/.ssh/authorized_keys" ] && [ -s /root/.ssh/authorized_keys ]; then
		install -m 600 -o "$DEPLOY_USER" -g "$DEPLOY_USER" /root/.ssh/authorized_keys "$home/.ssh/authorized_keys"
	fi
	echo "export PATH=\"\$PATH:$GS_ROOT/bin\"" >/etc/profile.d/grid-scanner.sh
	# Keys only, but never on a server where no key is in place yet (that would lock everyone out).
	if [ -s "$home/.ssh/authorized_keys" ]; then
		cat >/etc/ssh/sshd_config.d/10-grid-scanner.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
		sshd -t
		systemctl reload ssh 2>/dev/null || systemctl restart ssh
	else
		log "WARNING: no SSH key for $DEPLOY_USER or root; password login left as it is"
	fi
}

directories() {
	log "directories"
	install -d -m 755 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$GS_ROOT" "$GS_ROOT/releases" "$GS_ROOT/bin"
	# Group-writable: editors save through a temporary file in the same directory. deploy (docker group) is
	# root-equivalent anyway, so this takes nothing away.
	install -d -m 770 -o root -g "$DEPLOY_USER" "$(dirname "$ENV_FILE")"
	install -d -m 755 "$DATA_DIR" "$DATA_DIR/exports"
	install -d -m 750 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$DATA_DIR/backups"
}

env_file() {
	# A new file gets the whole template (comments too); an existing one only the names it lacks.
	local fresh=false ip sslip line name value added=()
	[ -s "$ENV_FILE" ] || fresh=true
	touch "$ENV_FILE"
	chmod 600 "$ENV_FILE"
	chown "$DEPLOY_USER:$DEPLOY_USER" "$ENV_FILE"
	ip=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}')
	sslip=${ip:+${ip//./-}.sslip.io}
	while IFS= read -r line || [ -n "$line" ]; do
		if [ -z "$line" ] || [ "${line:0:1}" = "#" ]; then
			! $fresh || printf '%s\n' "$line" >>"$ENV_FILE"
			continue
		fi
		name=${line%%=*}
		grep -q "^$name=" "$ENV_FILE" && continue
		value=${line#*=}
		case "$value" in
		__GENERATE__) value=$(openssl rand -hex 24) ;;
		__SSLIP__) value=$sslip ;;
		esac
		printf '%s=%s\n' "$name" "$value" >>"$ENV_FILE"
		added+=("$name")
	done <"$here/server-env.template"
	log "env file $ENV_FILE: ${#added[@]} name(s) added${added:+ (${added[*]})}"
}

scripts_and_timers() {
	# Until the first deploy links bin/deploy to the deployed release's copy, this copy does the first deploy.
	if [ ! -L "$GS_ROOT/bin/deploy" ]; then
		install -m 755 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$here/deploy.sh" "$GS_ROOT/bin/deploy"
		install -m 644 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$here/lib.sh" "$GS_ROOT/bin/lib.sh"
	fi
	# Root-owned and outside the release tree, so a rollback to an older release keeps the CI key's forced command.
	install -m 755 -o root -g root "$here/ci-deploy.sh" /usr/local/bin/grid-scanner-ci-deploy
	if [ -d /run/systemd/system ]; then
		log "backup and restore-check timers"
		install -m 644 "$here"/systemd/grid-scanner-* /etc/systemd/system/
		systemctl daemon-reload
		systemctl enable --now grid-scanner-backup.timer grid-scanner-restore-check.timer >/dev/null
	fi
}

[ "$(id -u)" = 0 ] || {
	echo "run as root" >&2
	exit 1
}
if [ "${1:-}" != "--files-only" ]; then
	packages
	upgrades
	swap
	firewall
	deploy_user
fi
directories
env_file
scripts_and_timers

cat <<EOF

Done. Next, as $DEPLOY_USER (ssh $DEPLOY_USER@<server>):
  1. review $ENV_FILE: ENTSOE_API_TOKEN, USER_AGENT contact, DOMAIN (now ${DOMAIN:-$(sed -n 's/^DOMAIN=//p' "$ENV_FILE")}),
     CORS_ALLOWED_ORIGINS, ALERT_WEBHOOK_URL
  2. docker login ghcr.io -u <github user>   (a classic token with read:packages)
  3. $GS_ROOT/bin/deploy main
EOF
