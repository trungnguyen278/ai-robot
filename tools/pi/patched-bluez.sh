#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Ubuntu's bluez, rebuilt with the upstream fixes in ../third_party/bluez (ADR-0020).
# It runs as an ordinary user and never asks for root. How to use it: tools/pi/README.md.

set -euo pipefail

readonly PACKAGE=bluez
# Appended to Ubuntu's version: dpkg sorts the build above the version it comes from and
# below Ubuntu's next one (deb-version(7)). Raise the number when the patches change.
readonly SUFFIX=+local1
# Who the build's changelog entry names, unless DEBFULLNAME and DEBEMAIL are set.
readonly BUILDER_NAME="Local build"
readonly BUILDER_EMAIL=local-build@localhost

PATCHES=$(cd "$(dirname "${BASH_SOURCE[0]}")/../third_party/$PACKAGE" && pwd)
readonly PATCHES

usage() {
    cat >&2 <<EOF
Usage:
  ${0##*/} build DIR [VERSION]
      Build Ubuntu's $PACKAGE VERSION, by default the newest that apt's sources offer,
      with the patches in tools/third_party/$PACKAGE, into DIR, a new or empty folder.
      Then print the root commands that install and hold the packages installed here.
  ${0##*/} check
      Say whether the installed $PACKAGE is the patched build of Ubuntu's newest, and held.

Exit status: 0 done or up to date, 1 something to do (the message says what), 2 usage.
EOF
    exit 2
}

fail() {
    printf '%s\n' "$*" >&2
    exit 1
}

# The newest version of the source package that apt's sources offer.
newest() {
    local version found=""
    while read -r version; do
        if [[ -z $found ]] || dpkg --compare-versions "$version" gt "$found"; then
            found=$version
        fi
    done < <(apt-cache showsrc --only-source "$PACKAGE" 2>/dev/null | sed -n 's/^Version: //p')
    [[ -n $found ]] || fail "apt's sources offer no source package for $PACKAGE: enable deb-src (tools/pi/README.md)"
    printf '%s\n' "$found"
}

# One line per binary package of the source package installed here: its name and version.
installed() {
    dpkg-query -W -f='${Package} ${source:Package} ${db:Status-Status} ${Version}\n' 2>/dev/null |
        awk -v source="$PACKAGE" '$2 == source && $3 == "installed" { print $1, $4 }'
}

# Does patch $1 apply to the source tree here, without fuzz? With $2 = -R, in reverse.
applies() {
    patch -p1 --dry-run --force --silent --fuzz=0 ${2:+"$2"} <"$1" >/dev/null 2>&1
}

build() {
    local dir=$1 version=${2:-} patch name tree deb
    local -a patches names=() debs=() packages=()
    [[ -n $version ]] || version=$(newest)
    patches=("$PATCHES"/*.patch)
    [[ -e ${patches[0]} ]] || fail "no patch in $PATCHES"

    mkdir -p "$dir"
    dir=$(cd "$dir" && pwd)
    [[ -z $(ls -A "$dir") ]] || fail "$dir is not empty"
    cd "$dir"
    apt-get source --only-source "$PACKAGE=$version" || fail "apt could not fetch the source of $PACKAGE $version"
    tree=$(find . -mindepth 1 -maxdepth 1 -type d -name "$PACKAGE-*")
    [[ -n $tree && $tree != *$'\n'* ]] || fail "no single source tree in $dir"
    cd "$tree"

    mkdir -p debian/patches
    for patch in "${patches[@]}"; do
        name=$(basename "$patch")
        if applies "$patch" -R; then
            fail "Ubuntu's $PACKAGE $version already has $name: drop it (tools/pi/README.md)"
        fi
        applies "$patch" || fail "$name does not apply to Ubuntu's $PACKAGE $version: check whether Ubuntu fixed it otherwise, or port it"
        cp "$patch" debian/patches/
        printf '%s\n' "$name" >>debian/patches/series
        names+=("$name")
    done
    dpkg-source --before-build . # applies them, as dpkg-buildpackage would
    for name in "${names[@]}"; do
        grep -qxF "$name" .pc/applied-patches || fail "$name was not applied"
    done

    local -a entry=(dch --newversion "$version$SUFFIX" --no-auto-nmu --force-distribution
        --distribution "$(dpkg-parsechangelog -S Distribution)")
    for patch in "${patches[@]}"; do
        DEBFULLNAME=${DEBFULLNAME:-$BUILDER_NAME} DEBEMAIL=${DEBEMAIL:-$BUILDER_EMAIL} "${entry[@]}" \
            "Apply upstream commit $(sed -n '1s/^From \([0-9a-f]\{12\}\).*/\1/p' "$patch"): $(sed -n 's/^Subject: //p' "$patch" | head -n 1)"
        entry=(dch --append)
    done

    DEB_BUILD_OPTIONS="parallel=$(nproc) ${DEB_BUILD_OPTIONS:-}" dpkg-buildpackage -b -us -uc ||
        fail "the build of $PACKAGE $version$SUFFIX failed"

    # Package files are named without the version's epoch.
    while read -r name _; do
        deb=$(find "$dir" -maxdepth 1 -name "${name}_${version#*:}${SUFFIX}_*.deb" -print -quit)
        [[ -n $deb ]] || fail "the build made no $name, which is installed here"
        debs+=("$deb")
        packages+=("$name")
    done < <(installed)
    printf '\nBuilt %s %s%s in %s.\n' "$PACKAGE" "$version" "$SUFFIX" "$dir"
    if ((${#packages[@]})); then
        printf 'To install it here and hold it, as root:\n  apt-get install %s\n  apt-mark hold %s\n' \
            "${debs[*]}" "${packages[*]}"
    fi
}

check() {
    local latest line name version status=0
    local -a lines
    latest=$(newest)
    mapfile -t lines < <(installed)
    ((${#lines[@]})) || fail "$PACKAGE is not installed"
    for line in "${lines[@]}"; do
        read -r name version <<<"$line"
        if [[ $version == "$latest$SUFFIX" ]]; then
            printf "%s %s is the patched build of Ubuntu's newest.\n" "$name" "$version"
        else
            printf "%s %s is not the patched build of Ubuntu's newest, %s: build it (tools/pi/README.md).\n" \
                "$name" "$version" "$latest"
            status=1
        fi
        if [[ -z $(apt-mark showhold "$name") ]]; then
            printf '%s is not held: an update from Ubuntu would replace it.\n' "$name"
            status=1
        fi
    done
    return "$status"
}

case "${1:-}:$#" in
build:2 | build:3) build "${@:2}" ;;
check:1) check ;;
*) usage ;;
esac
