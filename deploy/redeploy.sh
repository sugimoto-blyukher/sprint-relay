#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
    cat <<'HELP'
Usage: sudo bash deploy/redeploy.sh [--yes] [--start-worker codex]

Install the checked-out SprintRelay source on the local systemd host.
Back up state, configuration and unit files. Preserve the former venv.
A failed rollout restores the previous application and units when possible.
The optional --start-worker codex starts the preconfigured Codex socket.
HELP
}

yes=false
start_codex=false
while (( $# )); do
    case "$1" in
        --help|-h) usage; exit 0 ;;
        --yes) yes=true; shift ;;
        --start-worker)
            [[ ${2:-} == codex ]] || { usage >&2; exit 2; }
            start_codex=true; shift 2 ;;
        *) usage >&2; exit 2 ;;
    esac
done

SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=deploy/common.sh
source "$SOURCE/deploy/common.sh"
require_host
require_root_directory "$CONFIG_DIR"
getent passwd sprintrelay >/dev/null || die "Run deploy/setup.sh for first-time provisioning."
[[ -r $CONFIG_DIR/config.toml && -f $CONFIG_DIR/core.env ]] ||
    die "Prepare $CONFIG_DIR/config.toml and core.env first."
[[ ! -L $CONFIG_DIR/core.env && $(stat -c '%u:%a' -- "$CONFIG_DIR/core.env") == 0:600 ]] ||
    die "core.env must be a root-owned regular file with mode 0600."
if [[ ! -d $APP ]]; then install -d -m 0755 "$APP"; fi
require_root_directory "$APP"
if [[ ! -d $APP/.venvs ]]; then install -d -m 0755 "$APP/.venvs"; fi
require_root_directory "$APP/.venvs"
if [[ ! -d $BACKUPS ]]; then install -d -m 0700 "$BACKUPS"; fi
require_root_directory "$BACKUPS"
[[ $(stat -c '%a' -- "$BACKUPS") == 700 ]] || die "$BACKUPS must have mode 0700."
if ! $yes; then
    confirm "Deploy $SOURCE to $APP and restart SprintRelay Core?" || exit 0
fi

umask 077
exec 9>"$LOCK_FILE"
flock -n 9 || die "Another SprintRelay deployment is in progress."
cd /
release=$(mktemp -d "$APP/.venvs/release.XXXXXXXX")
chmod 0755 "$release"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
cp -R -- "$SOURCE/relay" "$SOURCE/pyproject.toml" "$work/"
for unit in "${UNITS[@]}"; do
    install -m 0644 "$SOURCE/deploy/$unit" "$work/$unit"
done

# Finish building and validate the candidate before stopping a live service.
(
    # The release contains only installed code. Service UIDs need read access.
    umask 022
    python3 -m venv "$release"
    "$release/bin/python" -I -m pip install --disable-pip-version-check --no-compile "$work"
)
as_core "$release/bin/python" check

backup=$(mktemp -d "$BACKUPS/redeploy.XXXXXXXX")
cp -a -- "$CONFIG_DIR" "$backup/config"
mkdir "$backup/units"
for unit in "${UNITS[@]}"; do
    if [[ -e $UNIT_DIR/$unit || -L $UNIT_DIR/$unit ]]; then
        cp -a -- "$UNIT_DIR/$unit" "$backup/units/"
    fi
done

core_active=false
systemctl is-active --quiet sprintrelay.service && core_active=true
active_sockets=()
for provider in "${PROVIDERS[@]}"; do
    socket="sprintrelay-worker@$provider.socket"
    if systemctl is-active --quiet "$socket"; then active_sockets+=("$socket"); fi
done
target_sockets=("${active_sockets[@]}")
if $start_codex && [[ " ${target_sockets[*]} " != *" sprintrelay-worker@codex.socket "* ]]; then
    target_sockets+=(sprintrelay-worker@codex.socket)
fi

old_env="$APP/.venvs/previous.${backup##*/}"
env_changed=false
units_changed=false
core_started=false

stop_gateways() {
    local provider unit
    for provider in "${PROVIDERS[@]}"; do
        for unit in "sprintrelay-worker@$provider.socket" "sprintrelay-worker@$provider.service"; do
            if systemctl is-active --quiet "$unit"; then systemctl stop "$unit" || return; fi
        done
    done
}

rollback() {
    local rc=$1 unit ok=true
    trap - ERR INT TERM
    set +e
    printf 'Rollout interrupted. Restoring application and systemd units from %s.\n' "$backup" >&2
    systemctl stop sprintrelay.service || ok=false
    stop_gateways || ok=false
    if $env_changed; then
        if [[ -L $APP/.venv && $(readlink -- "$APP/.venv") == "$release" ]]; then
            rm -- "$APP/.venv" || ok=false
        fi
        if [[ -e $old_env || -L $old_env ]]; then
            mv -T -- "$old_env" "$APP/.venv" || ok=false
        fi
    fi
    if $units_changed; then
        for unit in "${UNITS[@]}"; do
            rm -f -- "$UNIT_DIR/$unit" || ok=false
            if [[ -e $backup/units/$unit || -L $backup/units/$unit ]]; then
                cp -a -- "$backup/units/$unit" "$UNIT_DIR/" || ok=false
            fi
        done
        systemctl daemon-reload || ok=false
    fi
    if $ok; then
        for unit in "${active_sockets[@]}"; do systemctl start "$unit" || ok=false; done
        # The new Core may have migrated the database or performed work. Keep
        # state and allow an operator to verify it before starting the old Core.
        if $core_active && ! $core_started; then systemctl start sprintrelay.service || ok=false; fi
    fi
    if ! $ok || $core_started; then
        printf 'Review service status and database compatibility before restarting Core. Backup: %s\n' "$backup" >&2
    fi
    exit "$rc"
}

trap 'rollback $?' ERR
trap 'rollback 130' INT
trap 'rollback 143' TERM
if $core_active; then systemctl stop sprintrelay.service; fi
stop_gateways
# All SprintRelay writers are now stopped. Copy any SQLite sidecars together.
if [[ -d $STATE ]]; then cp -a -- "$STATE" "$backup/state"; fi
as_core "$release/bin/python" migrate --dry-run

# Put the old environment on the same filesystem. Restoring its original
# path keeps old venv scripts usable even if they have absolute shebangs.
env_changed=true
if [[ -e $APP/.venv || -L $APP/.venv ]]; then mv -T -- "$APP/.venv" "$old_env"; fi
ln -s -- "$release" "$APP/.venv"
units_changed=true
for unit in "${UNITS[@]}"; do
    install -m 0644 "$work/$unit" "$UNIT_DIR/$unit"
done
systemctl daemon-reload
for socket in "${target_sockets[@]}"; do systemctl start "$socket"; done
core_started=true
systemctl start sprintrelay.service
# A Type=simple service can be active before Discord and the listener are ready.
# Check the real loopback endpoint without reading or sending credentials.
"$release/bin/python" -I - "$CONFIG_DIR/config.toml" <<'PYTHON'
import http.client
import json
import sys
import time
import tomllib

with open(sys.argv[1], "rb") as config:
    port = tomllib.load(config).get("mcp", {}).get("local_port", 8371)
deadline = time.monotonic() + 45
while time.monotonic() < deadline:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        connection.request("GET", "/mcp")
        response = connection.getresponse()
        if response.status == 401 and json.loads(response.read(1024)) == {"error": "invalid_token"}:
            break
    except (OSError, http.client.HTTPException, ValueError):
        pass
    finally:
        connection.close()
    time.sleep(1)
else:
    raise SystemExit("The authenticated local MCP listener did not become ready.")
PYTHON
systemctl is-active --quiet sprintrelay.service
trap - ERR INT TERM
printf 'Installed %s\nBackup: %s\n' "$release" "$backup"
printf 'Verify Discord and both Codex probes using docs/deployment.md.\n'
