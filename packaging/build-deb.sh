#!/bin/bash
# Builds the Debian package: the bridge together with its own copies of shairport-sync and
# nqptp, so that installing it needs no compiler and touches nothing else on the machine.
#
#   packaging/build-deb.sh [--install-build-packages]
#
# Run it on the kind of system the package is for (Debian 13, Debian 12, ...): the programs are
# linked against that system's libraries. The package lands in dist/.
#
#   BUILD_CACHE   where the built programs are kept between runs (default: .build)
set -euo pipefail

SHAIRPORT_VERSION=5.6.3     # needs 5 or later: D-Bus name tags, several instances side by side
NQPTP_VERSION=1.2.9
SHAIRPORT_SOURCE=https://github.com/mikebrady/shairport-sync.git
NQPTP_SOURCE=https://github.com/mikebrady/nqptp.git
BUILD_PACKAGES="git ca-certificates build-essential autoconf automake libtool pkg-config xxd dpkg-dev
    libpopt-dev libconfig-dev libavahi-client-dev libssl-dev libglib2.0-dev
    libplist-dev libplist-utils libsodium-dev libgcrypt20-dev uuid-dev
    libavcodec-dev libavformat-dev libavutil-dev libswresample-dev"
# What the bridge itself runs: busctl comes with systemd, the hooks use curl.
RUN_DEPENDS="python3 (>= 3.11), ffmpeg, curl, avahi-daemon, dbus, systemd, adduser"

root=$(cd "$(dirname "$0")/.." && pwd)
cache=${BUILD_CACHE:-$root/.build}
say() { printf '\n== %s\n' "$*"; }
fail() { printf 'build-deb.sh: %s\n' "$*" >&2; exit 1; }

if [ "${1:-}" = "--install-build-packages" ]; then
    missing=""
    for package in $BUILD_PACKAGES; do
        dpkg-query -W -f '${Status}' "$package" 2>/dev/null | grep -q "ok installed" || missing="$missing $package"
    done
    if [ -n "$missing" ]; then
        say "Installing build packages:$missing"
        apt-get update -q
        # shellcheck disable=SC2086
        DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends $missing
    fi
fi
for tool in git autoreconf make dpkg-deb dpkg-shlibdeps; do
    command -v "$tool" >/dev/null || fail "$tool is missing; run me with --install-build-packages (as root)"
done

# ---------------------------------------------------------------- the two programs

build() {   # build <name> <source> <version> <configure arguments...>; leaves $cache/<name>-<version>/<name>
    local name=$1 source=$2 version=$3 dir; shift 3
    dir=$cache/$name-$version
    [ -x "$dir/$name" ] && return
    say "Building $name $version (a few minutes on a Raspberry Pi)"
    rm -rf "$dir"
    git -c advice.detachedHead=false clone -q --depth 1 --branch "$version" "$source" "$dir"
    (
        cd "$dir"
        autoreconf -fi >/dev/null
        ./configure "$@" >/dev/null
        make -j"$(nproc)" >/dev/null
    )
    [ -x "$dir/$name" ] || fail "$name didn't build"
}

mkdir -p "$cache"
build nqptp "$NQPTP_SOURCE" "$NQPTP_VERSION"
# No audio output of its own: the bridge takes the sound from a pipe.
build shairport-sync "$SHAIRPORT_SOURCE" "$SHAIRPORT_VERSION" --sysconfdir=/etc \
    --with-airplay-2 --with-pipe --with-metadata --with-dbus-interface --with-avahi --with-ssl=openssl

# ---------------------------------------------------------------- the package's files

version=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$root/raumfeld_airplay/__init__.py")
[ -n "$version" ] || fail "no version in raumfeld_airplay/__init__.py"
# shellcheck disable=SC1091
. /etc/os-release
version="$version+$ID$VERSION_ID"     # the package only fits the system it was built on
arch=$(dpkg --print-architecture)

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
pkg=$work/pkg
app=$pkg/opt/raumfeld-airplay

install -d "$app/bin" "$pkg/DEBIAN" "$pkg/usr/lib/systemd/system" "$pkg/usr/share/dbus-1/system.d" \
    "$pkg/usr/share/doc/raumfeld-airplay"
cp -R "$root/raumfeld_airplay" "$app/"
find "$app" -name __pycache__ -prune -exec rm -rf {} +
install -m 755 "$root/event-hook" "$root/volume-hook" "$app/"
install -m 755 "$cache/nqptp-$NQPTP_VERSION/nqptp" "$cache/shairport-sync-$SHAIRPORT_VERSION/shairport-sync" "$app/bin/"
strip "$app/bin/nqptp" "$app/bin/shairport-sync"
install -m 644 "$root/raumfeld-airplay.service" "$pkg/usr/lib/systemd/system/"
install -m 644 "$root/packaging/dbus-policy.conf" "$pkg/usr/share/dbus-1/system.d/raumfeld-airplay.conf"
install -m 644 "$root/README.md" "$pkg/usr/share/doc/raumfeld-airplay/"
sed -e "s/@SHAIRPORT_VERSION@/$SHAIRPORT_VERSION/" -e "s/@NQPTP_VERSION@/$NQPTP_VERSION/" \
    "$root/packaging/copyright" >"$pkg/usr/share/doc/raumfeld-airplay/copyright"
install -m 755 "$root/packaging/postinst" "$root/packaging/prerm" "$root/packaging/postrm" "$pkg/DEBIAN/"

# The libraries the two programs are linked against, as package dependencies.
mkdir -p "$work/shlibs/debian"
printf 'Source: raumfeld-airplay\n\nPackage: raumfeld-airplay\nArchitecture: any\n' >"$work/shlibs/debian/control"
libraries=$(cd "$work/shlibs" && dpkg-shlibdeps -O "$app/bin/nqptp" "$app/bin/shairport-sync" 2>/dev/null |
    sed -n 's/^shlibs:Depends=//p')
[ -n "$libraries" ] || fail "couldn't work out the libraries the programs need"

cat >"$pkg/DEBIAN/control" <<CONTROL
Package: raumfeld-airplay
Version: $version
Architecture: $arch
Maintainer: raumfeld-airplay <noreply@example.invalid>
Section: sound
Priority: optional
Installed-Size: $(du -sk "$pkg" | cut -f1)
Depends: $RUN_DEPENDS, $libraries
Description: AirPlay 2 speakers for Teufel Raumfeld rooms
 Makes every room of a Raumfeld system show up as an AirPlay 2 speaker. Rooms
 selected together play in sync, lossless, with pause, play, volume and track
 titles working in both directions.
 .
 Contains its own copies of shairport-sync $SHAIRPORT_VERSION and nqptp $NQPTP_VERSION.
CONTROL

mkdir -p "$root/dist"
deb=$root/dist/raumfeld-airplay_${version}_${arch}.deb
dpkg-deb --root-owner-group --build "$pkg" "$deb" >/dev/null
say "Built $deb"
dpkg-deb --info "$deb" | sed -n 's/^ \(Version\|Architecture\|Depends\|Installed-Size\):/\1:/p'
