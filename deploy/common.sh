# Shared by the operator scripts. Run the entry points from a trusted checkout.
APP=/opt/sprintrelay
CONFIG_DIR=/etc/sprintrelay
STATE=/var/lib/sprintrelay
UNIT_DIR=/etc/systemd/system
BACKUPS=/var/backups/sprintrelay
LOCK_FILE=/run/lock/sprintrelay-deploy.lock
UNITS=(sprintrelay.service sprintrelay-worker@.service sprintrelay-worker@.socket)
PROVIDERS=(codex claude_code antigravity)

die() { printf 'SprintRelay: %s\n' "$*" >&2; exit 1; }

confirm() {
    local answer
    printf '%s [y/N] ' "$1" >/dev/tty
    IFS= read -r answer </dev/tty
    [[ $answer == y || $answer == Y || $answer == yes ]]
}

require_host() {
    [[ $EUID == 0 ]] || die "Run this script with sudo."
    [[ $(uname -s) == Linux && -d /run/systemd/system ]] ||
        die "This installer requires a Linux host running systemd."
    local command
    for command in python3 systemctl install flock runuser; do
        command -v "$command" >/dev/null || die "Install $command first."
    done
    python3 -I -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' ||
        die "Python 3.11 or later is required."
}

require_root_directory() {
    local path=$1 owner mode
    [[ -d $path && ! -L $path ]] || die "Expected a real directory at $path."
    read -r owner mode < <(stat -c '%u %a' -- "$path")
    [[ $owner == 0 && $((8#$mode & 022)) == 0 ]] ||
        die "$path must be root-owned and writable only by root."
}

ensure_account() {
    local name=$1 home=$2 uid gid owner configured_home
    getent group "$name" >/dev/null || groupadd --system "$name"
    id -u "$name" >/dev/null 2>&1 ||
        useradd --system --gid "$name" --home-dir "$home" --shell /usr/sbin/nologin "$name"
    IFS=: read -r owner _ uid gid _ configured_home _ < <(getent passwd "$name")
    [[ $owner == "$name" && $uid != 0 && $configured_home == "$home" &&
       $gid == "$(getent group "$name" | cut -d: -f3)" ]] ||
        die "Existing $name account has an unexpected UID, group or home."
}

as_core() {
    runuser -u sprintrelay -- "$1" -I -m relay "${@:2}" \
        --config "$CONFIG_DIR/config.toml" --state-dir "$STATE"
}
