# SPDX-License-Identifier: MIT
"""The connection profiles NetworkManager keeps, and those the robot made: set-up marks
each profile it makes, so that the factory reset forgets those and no other
(docs/decisions/0023-factory-reset.md).

The mark is in the profile's ``user`` setting, which NetworkManager keeps for its users
and otherwise ignores: string keys, each a name of dot-separated parts, to string values
(nm-settings-dbus(5), ``user.data``; nm-setting-user.c on NetworkManager's main
branch, read 2026-11-19).
"""

from __future__ import annotations

from dbus_fast import Variant
from dbus_fast.aio import MessageBus

from ai_robot.adapters.dbus import CallError, call, gone, owner_of

# NetworkManager's D-Bus API (networkmanager.dev/docs/api/latest/).
NM = "org.freedesktop.NetworkManager"
SETTINGS_PATH = "/org/freedesktop/NetworkManager/Settings"
SETTINGS = f"{NM}.Settings"
CONNECTION = f"{NM}.Settings.Connection"
# A profile another user keeps to themselves: ListConnections lists every profile, and
# GetSettings refuses a caller outside a profile's connection.permissions with this
# (impl_settings_list_connections in nm-settings.c, auth_start in
# nm-settings-connection.c, NM_SETTINGS_ERROR in nm-errors.c; NetworkManager 1.54.0, read
# 2026-11-21). It answers the same when the bus does not tell it the caller's user within
# two seconds (_bus_get_unix_user in nm-dbus-manager.c), which the robot cannot tell apart.
PRIVATE = f"{SETTINGS}.PermissionDenied"

MARK = "ai-robot.setup"  # the key of the robot's mark in user.data
MADE = "joined"  # its value: set-up made the profile to join the network

Profile = dict[str, dict[str, Variant]]  # a profile's settings, by setting name


def mark(profile: Profile) -> None:
    """Mark ``profile``, a new one, as made by the robot."""
    profile["user"] = {"data": Variant("a{ss}", {MARK: MADE})}


def made(profile: Profile) -> bool:
    """Whether the robot made ``profile``."""
    data = profile.get("user", {}).get("data")
    return data is not None and data.value.get(MARK) == MADE


async def listed(bus: MessageBus, nm: str) -> list[tuple[str, Profile]]:
    """Every profile the NetworkManager ``nm`` keeps that the robot may read, with its
    settings (secrets left out). ``nm`` is a unique name: a profile's path means nothing
    to another NetworkManager, which numbers the profiles anew as it starts. It passes
    over a profile deleted as it lists, and one another user keeps private;
    ``ConnectionError`` for any other failure, such as ``nm`` that stops, so that a part
    of the profiles is never taken for all of them."""
    (paths,) = await call(bus, nm, SETTINGS_PATH, SETTINGS, "ListConnections")
    found = []
    for path in paths:
        try:
            (settings,) = await call(bus, nm, path, CONNECTION, "GetSettings")
        except CallError as error:
            if gone(error) or error.name == PRIVATE:
                continue
            raise
        found.append((path, settings))
    return found


async def forget_made(bus: MessageBus) -> int:
    """Delete each profile the robot made; how many. Each call goes to the NetworkManager
    that ran as it began, so that a path never reaches another. ``ConnectionError`` if
    NetworkManager does not run, or stops, or a profile cannot be deleted."""
    nm = await owner_of(bus, NM)
    gone = 0
    for path, profile in await listed(bus, nm):
        if made(profile):
            await call(bus, nm, path, CONNECTION, "Delete")
            gone += 1
    return gone


def ssid_of(profile: Profile) -> bytes | None:
    """The network name of a Wi-Fi profile, as bytes; ``None`` for another kind."""
    wireless = profile.get("802-11-wireless")
    if wireless and "ssid" in wireless:
        return bytes(wireless["ssid"].value)
    return None
