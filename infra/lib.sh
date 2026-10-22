# Shared by the VPS scripts (deploy.sh, compose.sh, scanner.sh, backup.sh, restore-check.sh); sourced, not run.
# Paths default to the server layout made by bootstrap.sh; the rehearsal overrides them (infra/README.md, "VPS").
# shellcheck shell=bash

GS_ROOT=${GS_ROOT:-/opt/grid-scanner}
ENV_FILE=${ENV_FILE:-/etc/grid-scanner/env}
COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME:-grid-scanner}
export COMPOSE_PROJECT_NAME

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() {
	log "ERROR: $*" >&2
	exit 1
}

# The value of NAME in the env file (last assignment wins; surrounding quotes removed). Read, never sourced.
env_value() {
	local v
	v=$(sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1)
	v=${v%\"}
	v=${v#\"}
	v=${v%\'}
	v=${v#\'}
	printf '%s' "$v"
}

data_dir() { local d; d=$(env_value DATA_DIR); printf '%s' "${d:-/var/lib/grid-scanner}"; }

# docker compose for the deployed release: its compose file, the env file for interpolation, and its image tag.
# RELEASE_DIR and TAG override them while deploy.sh brings up a new release.
compose() {
	local dir=${RELEASE_DIR:-$GS_ROOT/current}
	[ -f "$dir/docker-compose.prod.yml" ] || die "no release at $dir (deploy first)"
	TAG=${TAG:-$(cat "$dir/TAG")} docker compose --project-name "$COMPOSE_PROJECT_NAME" --env-file "$ENV_FILE" \
		-f "$dir/docker-compose.prod.yml" "$@"
}

# Post TITLE and TEXT to ALERT_WEBHOOK_URL when it is set (same formats as scanner.runtime.alerts: Slack or
# Discord JSON, or plain text with a Title header). Never fails the caller and never prints the URL.
alert() {
	local url fmt
	url=$(env_value ALERT_WEBHOOK_URL)
	[ -n "$url" ] || return 0
	fmt=$(env_value ALERT_WEBHOOK_FORMAT)
	if [ "$fmt" = slack ] || { [ "${fmt:-auto}" = auto ] && [[ $url == *hooks.slack.com* ]]; }; then
		python3 -c 'import json, sys; print(json.dumps({"text": "*" + sys.argv[1] + "*\n" + sys.argv[2]}))' "$1" "$2" |
			curl -fsS -m 20 -o /dev/null -H 'Content-Type: application/json' --data-binary @- "$url" ||
			log "alert could not be sent"
	elif [ "$fmt" = discord ] || { [ "${fmt:-auto}" = auto ] && [[ $url == https://discord.com/api/webhooks/* || $url == https://discordapp.com/api/webhooks/* ]]; }; then
		python3 -c 'import json, sys; c = "**" + sys.argv[1] + "**\n" + sys.argv[2]; print(json.dumps({"content": c[:2000], "allowed_mentions": {"parse": []}}))' "$1" "$2" |
			curl -fsS -m 20 -o /dev/null -H 'Content-Type: application/json' --data-binary @- "$url" ||
			log "alert could not be sent"
	else
		curl -fsS -m 20 -o /dev/null -H "Title: $1" --data-binary "$2" "$url" || log "alert could not be sent"
	fi
}
