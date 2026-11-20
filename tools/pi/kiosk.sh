#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# The robot's kiosk: Ubuntu Frame and Chromium show the screen that the robot's software
# serves, and the screen sleeps and wakes with the robot (docs/decisions/0021-sleep-and-wake.md).
# It runs as the robot's user and never asks for root. How to use it: tools/pi/README.md.

set -euo pipefail

KIOSK=$(cd "$(dirname "${BASH_SOURCE[0]}")/kiosk" && pwd)
TREE=$(cd "$KIOSK/../../.." && pwd)
readonly KIOSK TREE

readonly FRAME=ubuntu-frame
readonly CHROMIUM=chromium
# Where Frame's configure hook writes the config option (Frame 457: $SNAP_DATA/frame.config),
# between these two lines.
readonly FRAME_CONFIG_FILE=/var/snap/$FRAME/current/frame.config
readonly FRAME_CONFIG_BEGIN='# ** Options supplied through config (begin) **'
readonly FRAME_CONFIG_END='# ** Options supplied through config (end) **'
readonly UNITS=etc/systemd/system
readonly HELPER=usr/local/libexec/ai-robot-wake
readonly HELPER_SOURCE=$TREE/software/src/ai_robot/adapters/wake/helper.py
readonly PYTHON=$TREE/software/.venv/bin/python
readonly CONFIG_DIR=$TREE/software/config
readonly UINPUT_GROUP=uinput
# The group that may read the touch screens, which only the helper has: it lights the dark
# screen when one is touched.
readonly TOUCH_GROUP=touchscreen
# Groups the robot's user must not be in: either lets it type into the kiosk's Chromium,
# which runs as root. uinput creates input devices; input writes events into existing
# ones, such as the touch screen's (drivers/input/evdev.c, evdev_write).
readonly -a FORBIDDEN_GROUPS=(uinput input)
# The units install starts, which must run and start at boot, and the snaps' daemons.
readonly -a OWN_UNITS=(ai-robot-wake.socket ai-robot-wake.service ai-robot.service)
readonly -a SNAP_UNITS=("snap.$FRAME.daemon.service" "snap.$CHROMIUM.daemon.service")

# What the robot's configuration says, one value a line: the screen's adapter, the display
# power's adapter, the helper's socket and the screen's port.
readonly READ_CONFIG='
import sys
from pathlib import Path

from ai_robot.core.config import load_config

ports = load_config(sys.argv[1], Path(sys.argv[2]))["ports"]
print(ports["screen"]["adapter"])
print(ports["display_power"]["adapter"])
print(ports["display_power"]["helper"]["path"])
print(ports["screen"]["web"]["port"])
'

usage() {
    cat >&2 <<EOF
Usage, as the robot's user:
  ${0##*/} install PROFILE DIR
      Write the kiosk's files for the robot of this tree, run with PROFILE, into DIR, a new
      or empty folder, laid out as they go on this machine. Then print the root commands
      that put them in place and restart the helper, the robot, Frame and Chromium.
      The profile must light the screen through the helper:
      [ports.display_power] adapter = "helper".
  ${0##*/} check PROFILE
      Say whether this machine's kiosk is as install sets it up.

Exit status: 0 done or as set up, 1 something to do (the messages say what), 2 usage.
EOF
    exit 2
}

fail() {
    printf '%s\n' "$*" >&2
    exit 1
}

# The robot's user and group, and what its configuration says; checks both.
prepare() {
    [[ $EUID -ne 0 ]] || fail "run it as the robot's user, not as root: it prints the root commands"
    user=$(id -un)
    group=$(id -gn)
    [[ -x $PYTHON ]] || fail "$PYTHON not found: set up the robot's software first (software/README.md)"
    snap list "$FRAME" "$CHROMIUM" >/dev/null 2>&1 ||
        fail "install the snaps first, as root: snap install $FRAME $CHROMIUM"
    local values screen adapter port
    values=$("$PYTHON" -I -c "$READ_CONFIG" "$profile" "$CONFIG_DIR") ||
        fail "cannot read the robot's configuration for this profile"
    { read -r screen && read -r adapter && read -r socket && read -r port; } <<<"$values" ||
        fail "the robot's configuration for this profile lacks a value kiosk.sh needs"
    [[ $screen == web ]] || fail 'the profile does not serve the screen to the kiosk: [ports.screen] adapter must be "web"'
    [[ $adapter == helper ]] ||
        fail 'the profile does not light the screen through the helper: set [ports.display_power] adapter = "helper", in software/config/local.toml (tools/pi/README.md)'
    url=http://127.0.0.1:$port/
    local value
    for value in "$user" "$group" "$TREE" "$profile" "$socket" "$url"; do
        [[ $value =~ ^[A-Za-z0-9._/:-]+$ ]] ||
            fail "the robot's user, group, tree, profile, helper socket or port holds a character the units cannot take"
    done
}

# Each file of tools/pi/kiosk/ that is installed: the file, then where it goes, from /.
files() {
    printf '%s %s\n' \
        70-ai-robot-uinput.rules etc/udev/rules.d/70-ai-robot-uinput.rules \
        70-ai-robot-touchscreen.rules etc/udev/rules.d/70-ai-robot-touchscreen.rules \
        ai-robot-wake.socket "$UNITS/ai-robot-wake.socket" \
        ai-robot-wake.service "$UNITS/ai-robot-wake.service" \
        ai-robot.service "$UNITS/ai-robot.service" \
        chromium.conf "$UNITS/snap.$CHROMIUM.daemon.service.d/ai-robot.conf" \
        frame-daemon.conf "$UNITS/snap.$FRAME.daemon.service.d/ai-robot.conf"
}

# File $1 of tools/pi/kiosk/, with this robot's values.
render() {
    sed -e "s|@USER@|$user|g" -e "s|@GROUP@|$group|g" -e "s|@TREE@|$TREE|g" \
        -e "s|@PROFILE@|$profile|g" -e "s|@SOCKET@|$socket|g" -e "s|@URL@|$url|g" "$KIOSK/$1"
}

# Frame's config option: the lines of frame.config that are not comments.
frame_config() {
    grep -v -e '^#' -e '^$' "$KIOSK/frame.config"
}

# The device nodes of the touch screens, one a line, as udev tells them apart.
touchscreens() {
    local node
    for node in /dev/input/event*; do
        [[ $(udevadm info --query=property --property=ID_INPUT_TOUCHSCREEN --value --name="$node" 2>/dev/null) == 1 ]] &&
            printf '%s\n' "$node"
    done
    return 0
}

in_group() {
    id -nG "$user" | tr ' ' '\n' | grep -qx -- "$1"
}

# Whether a process of the robot's user still holds group $1. A login reads the user's groups
# from the group database once (initgroups(3)), and its processes inherit them: one started
# before the user left the group keeps it until it ends, such as an ssh session or the user's
# systemd manager, which starts processes for it.
held_by_a_process() {
    local gid uid
    gid=$(getent group "$1" | cut -d: -f3) || return 1
    uid=$(id -u "$user")
    # Lines "/proc/PID/status:Uid:\tREAL\t..." and "/proc/PID/status:Groups:\tGID GID ...".
    # A process may end while it is read: grep then says nothing, and its status is not ours.
    { grep -s -e '^Uid:' -e '^Groups:' /proc/[0-9]*/status || true; } |
        awk -v uid="$uid" -v gid="$gid" '
            {
                split($0, parts, ":")
                file = parts[1]
                key = parts[2]
                sub(/^[^:]*:[^:]*:[ \t]*/, "")
                n = split($0, values, /[ \t]+/)
                if (key == "Uid") {
                    mine[file] = (values[1] == uid)
                } else if (mine[file]) {
                    for (i = 1; i <= n; i++) if (values[i] == gid) found = 1
                }
            }
            END { exit !found }'
}

install_kiosk() {
    local dir=$1
    if [[ -e $dir ]]; then
        [[ -d $dir && -z $(ls -A "$dir") ]] || fail "$dir is not an empty folder"
    fi
    mkdir -p "$dir"
    dir=$(cd "$dir" && pwd)
    [[ $dir =~ ^[A-Za-z0-9._/-]+$ ]] || fail "the folder's path holds a character the root commands cannot take"

    local template destination
    while read -r template destination; do
        mkdir -p "$dir/$(dirname "$destination")"
        render "$template" >"$dir/$destination"
    done < <(files)
    mkdir -p "$dir/$(dirname "$HELPER")"
    cp "$HELPER_SOURCE" "$dir/$HELPER"
    frame_config >"$dir/$FRAME.config"

    printf 'Wrote the kiosk for %s, profile %s, to %s.\n' "$user" "$profile" "$dir"
    printf 'To put it in place, as root:\n'
    as_root() { printf '  %s\n' "$*"; }
    as_root "getent group $UINPUT_GROUP >/dev/null || groupadd --system $UINPUT_GROUP"
    as_root "getent group $TOUCH_GROUP >/dev/null || groupadd --system $TOUCH_GROUP"
    while read -r template destination; do
        as_root "install -D -m 0644 $dir/$destination /$destination"
    done < <(files)
    as_root "install -D -m 0644 $dir/$HELPER /$HELPER"
    # The rules apply to /dev/uinput and the touch screens now, and from boot on.
    as_root "udevadm control --reload"
    as_root "modprobe uinput"
    as_root "udevadm trigger --settle --action=change --subsystem-match=misc --sysname-match=uinput"
    as_root "udevadm trigger --settle --action=change --subsystem-match=input --property-match=ID_INPUT_TOUCHSCREEN=1"
    local name held=0
    for name in "${FORBIDDEN_GROUPS[@]}"; do
        if in_group "$name"; then
            as_root "gpasswd --delete $user $name"
            held=1
        elif held_by_a_process "$name"; then
            held=1
        fi
    done
    as_root "systemctl daemon-reload"
    as_root "systemctl enable ${OWN_UNITS[*]}"
    # The socket is made again with its owner and mode, and then the helper.
    as_root "systemctl stop ai-robot-wake.service"
    as_root "systemctl restart ai-robot-wake.socket"
    as_root "systemctl start ai-robot-wake.service"
    # Without the groups it left, and with this tree's code.
    as_root "systemctl restart ai-robot.service"
    # Frame restarts when its config changes, and Chromium with it.
    as_root "snap set $FRAME daemon=true config=\"\$(cat $dir/$FRAME.config)\""
    as_root "snap set $CHROMIUM daemon=true url=$url"
    as_root "systemctl restart snap.$CHROMIUM.daemon.service"
    if ((held)); then
        printf 'Last, as root, end the sessions of %s: they keep the groups it left until they end,\n' "$user"
        printf 'and so does its systemd user manager. If you are logged in as %s, this ends your\n' "$user"
        printf 'session too; a reboot does the same.\n'
        as_root "loginctl terminate-user $user"
    fi
    printf 'Then check it, once the screen shows: %s check %s\n' "$0" "$profile"
}

# The config option that Frame runs with, from its config file.
frame_config_installed() {
    awk -v begin="$FRAME_CONFIG_BEGIN" -v end="$FRAME_CONFIG_END" \
        '$0 == end { inside = 0 } inside { print } $0 == begin { inside = 1 }' "$FRAME_CONFIG_FILE" 2>/dev/null
}

# The page that the kiosk's Chromium shows: the last argument of its command line.
chromium_url() {
    local pid
    pid=$(systemctl show -P MainPID "snap.$CHROMIUM.daemon.service")
    [[ $pid != 0 ]] && tr '\0' '\n' <"/proc/$pid/cmdline" 2>/dev/null | tail -n 1
}

check_kiosk() {
    local problems=0
    problem() {
        printf '%s\n' "$*" >&2
        problems=$((problems + 1))
    }
    [[ $(frame_config_installed) == "$(frame_config)" ]] ||
        problem "Ubuntu Frame's config is not tools/pi/kiosk/frame.config"
    [[ $(chromium_url) == "$url" ]] || problem "Chromium's kiosk does not show the robot's screen"

    local template destination
    while read -r template destination; do
        cmp -s <(render "$template") "/$destination" ||
            problem "/$destination is not tools/pi/kiosk/$template"
    done < <(files)
    cmp -s "$HELPER_SOURCE" "/$HELPER" || problem "/$HELPER is not this tree's helper"
    [[ $(stat -c '%U %G %a' /dev/uinput 2>/dev/null) == "root $UINPUT_GROUP 660" ]] ||
        problem "/dev/uinput is not for root and the $UINPUT_GROUP group alone"
    local node
    while read -r node; do
        [[ $(stat -c '%U %G %a' "$node" 2>/dev/null) == "root $TOUCH_GROUP 640" ]] ||
            problem "$node, a touch screen, is not for root, and the $TOUCH_GROUP group to read"
    done < <(touchscreens)

    local name
    for name in "${FORBIDDEN_GROUPS[@]}"; do
        if in_group "$name"; then
            problem "the robot's user is in the $name group, which could type into the kiosk"
        elif held_by_a_process "$name"; then
            problem "a process of the robot's user still holds the $name group: as root, loginctl terminate-user $user, or reboot"
        fi
    done

    local unit
    for unit in "${OWN_UNITS[@]}" "${SNAP_UNITS[@]}"; do
        systemctl is-active --quiet "$unit" || problem "$unit is not running"
        systemctl is-enabled --quiet "$unit" || problem "$unit does not start at boot"
        [[ $(systemctl show -P NeedDaemonReload "$unit") == no ]] ||
            problem "$unit changed on disk since systemd read it: systemctl daemon-reload"
    done

    ((problems == 0)) || exit 1
    echo "The kiosk is as install sets it up."
}

case ${1:-} in
install)
    [[ $# -eq 3 ]] || usage
    profile=$2
    prepare
    install_kiosk "$3"
    ;;
check)
    [[ $# -eq 2 ]] || usage
    profile=$2
    prepare
    check_kiosk
    ;;
*) usage ;;
esac
