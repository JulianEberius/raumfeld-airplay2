#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Builds the bridge's package from this checkout and installs it, for working on the bridge or
# for a system there is no ready-made package for. Otherwise, install a released package:
#
#   sudo apt install ./raumfeld-airplay_*.deb
#
# Usage:
#
#   sudo ./install.sh                     build and install, or update to this checkout
#   sudo ./install.sh -- --max-volume 70  the same, and set the bridge's options
#   sudo ./install.sh uninstall           remove the bridge again
#   ./install.sh --on pi [...]            do any of these on another machine, over ssh
#
# The first run builds shairport-sync and nqptp, which takes a few minutes on a Raspberry Pi.
# Later runs reuse them.
set -euo pipefail

OPTIONS_FILE=/etc/default/raumfeld-airplay
export BUILD_CACHE=/var/cache/raumfeld-airplay-build

here=$(cd "$(dirname "$0")" && pwd)
say() { printf '\n== %s\n' "$*"; }
fail() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- on another machine

if [ "${1:-}" = "--on" ]; then
    [ $# -ge 2 ] || fail "--on needs the machine's ssh name"
    target=$2; shift 2
    say "Copying this checkout to $target and running install.sh there"
    remote='dir=$(mktemp -d) && trap "sudo rm -rf $dir" EXIT && tar xzf - -C "$dir" && sudo bash "$dir/install.sh"'
    for argument in "$@"; do remote="$remote $(printf '%q' "$argument")"; done
    COPYFILE_DISABLE=1 tar czf - --no-xattrs -C "$here" \
        --exclude .git --exclude __pycache__ --exclude .mypy_cache --exclude .build --exclude dist . |
        ssh "$target" "$remote"
    exit
fi

# ---------------------------------------------------------------- arguments

action=install
options=""
while [ $# -gt 0 ]; do
    case $1 in
        install|uninstall) action=$1 ;;
        --) shift; options="$*"; break ;;
        -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit ;;
        *) fail "don't know '$1'; see --help" ;;
    esac
    shift
done

[ "$(id -u)" = 0 ] || fail "run me with sudo"
command -v apt-get >/dev/null || fail "this needs a Debian-like system with apt; see README.md for others"

if [ "$action" = uninstall ]; then
    say "Removing the bridge"
    apt-get remove -y -q raumfeld-airplay
    echo "Its options and what it remembered about the rooms are kept; 'sudo apt purge raumfeld-airplay' removes them too."
    exit
fi

# ---------------------------------------------------------------- build and install

rm -rf "$here/dist"
"$here/packaging/build-deb.sh" --install-build-packages

if [ -n "$options" ]; then
    cat >"$OPTIONS_FILE" <<EOF
# Options of the AirPlay to Raumfeld bridge. For the list: cd /opt/raumfeld-airplay && python3 -m raumfeld_airplay --help
# After a change: sudo systemctl restart raumfeld-airplay
OPTIONS="$options"
EOF
fi

say "Installing the package"
DEBIAN_FRONTEND=noninteractive apt-get install -y -q --reinstall --no-install-recommends "$here"/dist/raumfeld-airplay_*.deb

# ---------------------------------------------------------------- did it work?

say "Waiting for the bridge to find the Raumfeld rooms"
status=""
for _ in $(seq 20); do
    sleep 1
    status=$(curl -s -m 2 http://127.0.0.1:8090/status || true)
    [ -n "$status" ] && break
done
systemctl is-active -q raumfeld-airplay || {
    journalctl -u raumfeld-airplay -n 20 --no-pager >&2
    fail "the bridge didn't stay up; its last messages are above"
}
# shellcheck disable=SC1090
. "$OPTIONS_FILE"
echo "Options: $OPTIONS   (in $OPTIONS_FILE)"
if [ -n "$status" ]; then
    echo "AirPlay speakers now offered:"
    printf '%s\n' "$status" | sed -n "s/^[^ ]*: room='\([^']*\)'.*/  \1/p"
else
    echo "No Raumfeld room found yet. The bridge keeps looking; watch it with:"
    echo "  journalctl -u raumfeld-airplay -f"
fi
