#!/usr/bin/env bash
set +x
set -Eeuo pipefail

if [[ ${1:-} == --help || ${1:-} == -h ]]; then
    printf '%s\n' 'Usage: sudo bash deploy/setup.sh' \
        'Configure a Linux systemd host interactively. Reuse existing configuration and credentials.'
    exit 0
fi
(( $# == 0 )) || { printf 'Usage: sudo bash deploy/setup.sh\n' >&2; exit 2; }

SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=deploy/common.sh
source "$SOURCE/deploy/common.sh"
require_host
if [[ -e $APP/.venv || -L $APP/.venv ]]; then
    die "An installation already exists. Use deploy/redeploy.sh."
fi
confirm "Provision SprintRelay on this host and start Core?" || exit 0

umask 077
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT

prompt() {
    printf '%s' "$1" >/dev/tty
    IFS= read -r REPLY </dev/tty
}

read_token() {
    local name=$1 token
    while true; do
        printf '%s (hidden): ' "$name" >/dev/tty
        if ! IFS= read -rs token </dev/tty; then die "Credential entry cancelled."; fi
        printf '\n' >/dev/tty
        if [[ $token =~ ^[A-Za-z0-9_.-]+$ ]]; then break; fi
        printf 'Enter a token without spaces or quotes.\n' >/dev/tty
    done
    printf '%s=%s\n' "$name" "$token"
}

ensure_account sprintrelay "$STATE"
if [[ ! -d $STATE ]]; then install -d -m 0700 -o sprintrelay -g sprintrelay "$STATE"; fi
[[ ! -L $STATE && $(stat -c '%U:%a' -- "$STATE") == sprintrelay:700 ]] ||
    die "$STATE must be a real, private directory owned by sprintrelay."
if [[ ! -d $CONFIG_DIR ]]; then install -d -m 0750 -g sprintrelay "$CONFIG_DIR"; fi
require_root_directory "$CONFIG_DIR"

if [[ ! -e $CONFIG_DIR/config.toml ]]; then
    prompt "Path to a prepared TOML (Enter to edit the template): "
    if [[ -n $REPLY ]]; then
        [[ -f $REPLY ]] || die "Configuration file not found."
        cp -- "$REPLY" "$work/config.toml"
    else
        cp -- "$SOURCE/config.example.toml" "$work/config.toml"
        printf 'Set the server, channel, member, calendar and repository values in the editor.\n' >/dev/tty
        if command -v nano >/dev/null; then editor=nano; else editor=vi; fi
        "$editor" "$work/config.toml" </dev/tty >/dev/tty
    fi
    if grep -Eq '"example/(relay-management|hackathon-product)"' "$work/config.toml"; then
        die "Replace the example repositories before installing."
    fi
    install -m 0640 -g sprintrelay "$work/config.toml" "$CONFIG_DIR/config.toml"
fi

if [[ ! -e $CONFIG_DIR/core.env ]]; then
    for name in DISCORD_TOKEN GITHUB_MANAGEMENT_TOKEN GITHUB_PRODUCT_TOKEN; do
        read_token "$name" >>"$work/core.env"
    done
    management=$(grep '^GITHUB_MANAGEMENT_TOKEN=' "$work/core.env")
    product=$(grep '^GITHUB_PRODUCT_TOKEN=' "$work/core.env")
    [[ ${management#*=} != "${product#*=}" ]] || die "Use two distinct GitHub tokens."
    unset management product
    install -m 0600 "$work/core.env" "$CONFIG_DIR/core.env"
    rm -- "$work/core.env"
fi

codex_ready=false
if confirm "Prepare the optional Codex worker with a reviewed native binary?"; then
    command -v bwrap >/dev/null || die "Install bubblewrap before provisioning a worker."
    [[ -x /usr/bin/bwrap ]] || die "Install bubblewrap at /usr/bin/bwrap."
    bwrap --help | awk '/--disable-userns/ { found=1 } END { exit !found }' ||
        die "Install bubblewrap with --disable-userns support."
    ensure_account sprintrelay-codex /var/lib/sprintrelay-codex
    account=/var/lib/sprintrelay-codex
    if [[ ! -d $account ]]; then
        install -d -m 0700 -o sprintrelay-codex -g sprintrelay-codex "$account"
    fi
    [[ ! -L $account && $(stat -c '%U:%a' -- "$account") == sprintrelay-codex:700 ]] ||
        die "$account must be a real private directory owned by sprintrelay-codex."
    image=/opt/sprintrelay-workers/codex
    for dir in /opt/sprintrelay-workers "$image"; do
        if [[ ! -d $dir ]]; then install -d -m 0755 "$dir"; fi
        require_root_directory "$dir"
    done
    if [[ ! -e $image/agent ]]; then
        prompt "Absolute path to the reviewed native Codex binary: "
        [[ $REPLY == /* && -f $REPLY && -x $REPLY ]] || die "Specify an executable native Codex file."
        [[ $(od -An -tx1 -N4 -- "$REPLY" | tr -d ' \n') == 7f454c46 ]] ||
            die "The worker requires a native ELF binary."
        install -m 0755 -- "$REPLY" "$image/agent"
    fi
    [[ ! -L $image/agent && $(stat -c '%u:%a' -- "$image/agent") == 0:755 ]] ||
        die "Expected a root-owned native worker executable with mode 0755."
    runuser -u sprintrelay-codex -- env -i HOME="$account" PATH=/usr/bin:/bin \
        "$image/agent" --version >"$work/version"
    [[ -s $work/version && $(wc -c <"$work/version") -le 161 ]] || die "Unexpected Codex version output."
    if [[ -e $image/version ]]; then
        cmp -s "$work/version" "$image/version" || die "Existing Codex version pin differs."
    else
        install -m 0644 "$work/version" "$image/version"
    fi
    printf '%s\n' "Configure credentials using the official login under sprintrelay-codex." \
        "Use the existing loopback-only Squid allowlist on port 3128." \
        "See docs/agents/README.md for login, proxy and isolation checks."
    if confirm "Have you completed login and verified Squid and systemd egress restrictions?"; then
        command -v ss >/dev/null || die "Install iproute2 to check the proxy listener."
        ss -Hltn 'sport = :3128' |
            awk '$4 == "127.0.0.1:3128" { found=1; next }
                 $4 != "[::1]:3128" { unexpected=1 }
                 END { exit (!found || unexpected) }' ||
            die "Expected a proxy listening only on loopback, including 127.0.0.1:3128."
        auth="$account/.codex/auth.json"
        [[ ! -L $account/.codex && -d $account/.codex &&
           $(stat -c '%U:%a' -- "$account/.codex") == sprintrelay-codex:700 ]] ||
            die "Expected a private .codex directory owned by sprintrelay-codex."
        [[ ! -L $auth && -f $auth && $(stat -c '%U:%a' -- "$auth") == sprintrelay-codex:600 ]] ||
            die "Complete official account login and secure auth.json before activating the worker."
        codex_ready=true
    fi
fi

options=(--yes)
if $codex_ready; then options+=(--start-worker codex); fi
bash "$SOURCE/deploy/redeploy.sh" "${options[@]}"
systemctl enable sprintrelay.service
if $codex_ready; then systemctl enable sprintrelay-worker@codex.socket; fi
printf 'Setup complete. Check the bot and commission the CLI using docs/deployment.md.\n'
