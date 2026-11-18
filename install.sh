#!/bin/bash
# Installs, updates or removes the AirPlay to Raumfeld bridge on a Debian-like Linux.
#
#   sudo ./install.sh                     install, or update to this checkout
#   sudo ./install.sh -- --max-volume 70  the same, and set the bridge's options
#   sudo ./install.sh uninstall           remove the bridge again
#   ./install.sh --on pi [...]            do any of these on another machine, over ssh
#
# Installing is safe to repeat. It keeps the bridge's options and what it remembers about the
# rooms, and builds shairport-sync and nqptp only if they are missing or unsuitable.
set -euo pipefail

SHAIRPORT_VERSION=5.6.3     # needs 5 or later: D-Bus name tags, several instances side by side
NQPTP_VERSION=1.2.9
SHAIRPORT_SOURCE=https://github.com/mikebrady/shairport-sync.git
NQPTP_SOURCE=https://github.com/mikebrady/nqptp.git
SHAIRPORT_FEATURES="AirPlay2 Avahi pipe metadata dbus"   # what `shairport-sync -V` has to list

APP_DIR=/opt/raumfeld-airplay
UNIT=/etc/systemd/system/raumfeld-airplay.service
OPTIONS_FILE=/etc/default/raumfeld-airplay
DEFAULT_OPTIONS="--format flac"
SERVICE_USER=shairport-sync
RUN_PACKAGES="python3 ffmpeg curl avahi-daemon"
BUILD_PACKAGES="git build-essential autoconf automake libtool pkg-config xxd systemd-dev
    libpopt-dev libconfig-dev libavahi-client-dev libssl-dev libglib2.0-dev
    libplist-dev libplist-utils libsodium-dev libgcrypt20-dev uuid-dev
    libavcodec-dev libavformat-dev libavutil-dev libswresample-dev"

here=$(cd "$(dirname "$0")" && pwd)
say() { printf '\n== %s\n' "$*"; }
fail() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- on another machine

if [ "${1:-}" = "--on" ]; then
    [ $# -ge 2 ] || fail "--on needs the machine's ssh name"
    target=$2; shift 2
    say "Copying this checkout to $target and running install.sh there"
    remote='dir=$(mktemp -d) && trap "rm -rf $dir" EXIT && tar xzf - -C "$dir" && sudo bash "$dir/install.sh"'
    for argument in "$@"; do remote="$remote $(printf '%q' "$argument")"; done
    COPYFILE_DISABLE=1 tar czf - --no-xattrs -C "$here" --exclude .git --exclude __pycache__ --exclude .mypy_cache . |
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
        -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit ;;
        *) fail "don't know '$1'; see --help" ;;
    esac
    shift
done

[ "$(id -u)" = 0 ] || fail "run me with sudo"
command -v systemctl >/dev/null || fail "this needs systemd"

# ---------------------------------------------------------------- uninstall

if [ "$action" = uninstall ]; then
    say "Removing the bridge"
    systemctl disable --now raumfeld-airplay 2>/dev/null || true
    rm -rf "$APP_DIR" "$UNIT" "$OPTIONS_FILE"
    systemctl daemon-reload
    cat <<EOF

The bridge is gone. Left in place, since other things may use them:
  shairport-sync and nqptp    /usr/local/bin, and the nqptp service
  what the bridge remembered  /var/lib/raumfeld-airplay
  packages installed for it   $RUN_PACKAGES
EOF
    exit
fi

# ---------------------------------------------------------------- packages

command -v apt-get >/dev/null || fail "no apt-get here. On other systems install by hand:
  $RUN_PACKAGES, nqptp $NQPTP_VERSION and shairport-sync $SHAIRPORT_VERSION (see README.md), then copy
  raumfeld_airplay/ and the hooks to $APP_DIR and raumfeld-airplay.service to $UNIT."

missing() {   # the packages among the arguments that aren't installed
    local package
    for package in "$@"; do
        dpkg-query -W -f '${Status}' "$package" 2>/dev/null | grep -q "ok installed" || printf '%s ' "$package"
    done
}

apt_install() {
    local wanted
    wanted=$(missing "$@")
    [ -n "$wanted" ] || return 0
    say "Installing packages: $wanted"
    apt-get update -q
    # shellcheck disable=SC2086
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends $wanted
}

# shellcheck disable=SC2086
apt_install $RUN_PACKAGES

python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' ||
    fail "the bridge needs Python 3.11 or later; this is $(python3 --version)"

# ---------------------------------------------------------------- nqptp and shairport-sync

shairport_suitable() {
    local version feature
    version=$(shairport-sync -V 2>/dev/null) || return 1
    [ "${version%%.*}" -ge 5 ] 2>/dev/null || return 1
    for feature in $SHAIRPORT_FEATURES; do
        case $version in *"-$feature"*) ;; *) return 1 ;; esac
    done
}

build() {   # build <name> <source> <version> <configure arguments...>
    local name=$1 source=$2 version=$3; shift 3
    say "Building $name $version (this takes a few minutes on a Raspberry Pi)"
    git clone -q --depth 1 --branch "$version" "$source" "$build_dir/$name"
    (
        cd "$build_dir/$name"
        autoreconf -fi >/dev/null
        ./configure "$@" >/dev/null
        make -j"$(nproc)" >/dev/null
        make install >/dev/null
    )
}

export PATH=/usr/local/bin:$PATH
need_nqptp=""; need_shairport=""
command -v nqptp >/dev/null || need_nqptp=yes
shairport_suitable || need_shairport=yes

if [ -n "$need_nqptp$need_shairport" ]; then
    # shellcheck disable=SC2086
    apt_install $BUILD_PACKAGES
    build_dir=$(mktemp -d /var/tmp/raumfeld-airplay-build.XXXXXX)
    trap 'rm -rf "$build_dir"' EXIT
    if [ -n "$need_nqptp" ]; then
        build nqptp "$NQPTP_SOURCE" "$NQPTP_VERSION" --with-systemd-startup
    fi
    if [ -n "$need_shairport" ]; then
        # No audio output of its own: the bridge takes the sound from a pipe. No service of its
        # own either: the bridge starts one instance per room.
        build shairport-sync "$SHAIRPORT_SOURCE" "$SHAIRPORT_VERSION" --sysconfdir=/etc \
            --with-airplay-2 --with-pipe --with-metadata --with-dbus-interface --with-avahi --with-ssl=openssl
    fi
    shairport_suitable || fail "the shairport-sync that was built doesn't report: $SHAIRPORT_FEATURES"
else
    say "nqptp and $(shairport-sync -V | cut -d- -f1,2) are already here"
fi

# ---------------------------------------------------------------- the bridge

say "Installing the bridge to $APP_DIR"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --user-group --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi
[ -f /etc/dbus-1/system.d/shairport-sync-dbus.conf ] || [ -f /usr/share/dbus-1/system.d/shairport-sync-dbus.conf ] ||
    fail "shairport-sync's D-Bus policy is missing; the speakers' keys could not reach the sender"

rm -rf "$APP_DIR/raumfeld_airplay"
install -d "$APP_DIR"
cp -R "$here/raumfeld_airplay" "$APP_DIR/"
find "$APP_DIR" -name __pycache__ -prune -exec rm -rf {} +
install -m 755 "$here/event-hook" "$here/volume-hook" "$APP_DIR/"
install -m 644 "$here/raumfeld-airplay.service" "$UNIT"

if [ -n "$options" ] || [ ! -f "$OPTIONS_FILE" ]; then
    cat >"$OPTIONS_FILE" <<EOF
# Options of the AirPlay to Raumfeld bridge. For the list: cd $APP_DIR && python3 -m raumfeld_airplay --help
# After a change: sudo systemctl restart raumfeld-airplay
OPTIONS="${options:-$DEFAULT_OPTIONS}"
EOF
fi

systemctl daemon-reload
systemctl reload dbus 2>/dev/null || true
systemctl enable -q --now avahi-daemon nqptp
systemctl enable -q raumfeld-airplay
systemctl restart raumfeld-airplay

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
