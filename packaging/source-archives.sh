#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Writes the source code of the programs inside the package to dist/, exactly as the package is
# built from it. They are published next to the packages: nqptp's licence, the GPL version 2,
# asks for the source to be offered together with the program.
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
# shellcheck source=packaging/versions.sh
. "$root/packaging/versions.sh"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$root/dist"
archive() {   # archive <name> <source> <version>
    git -c advice.detachedHead=false clone -q --depth 1 --branch "$3" "$2" "$work/$1"
    git -C "$work/$1" archive --format=tar.gz --prefix="$1-$3/" -o "$root/dist/$1-$3-source.tar.gz" HEAD
    echo "dist/$1-$3-source.tar.gz"
}
archive nqptp "$NQPTP_SOURCE" "$NQPTP_VERSION"
archive shairport-sync "$SHAIRPORT_SOURCE" "$SHAIRPORT_VERSION"
