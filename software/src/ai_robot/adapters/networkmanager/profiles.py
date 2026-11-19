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

from ai_robot.adapters.dbus import call

# NetworkManager's D-Bus API (networkmanager.dev/docs/api/latest/).
NM = "org.freedesktop.NetworkManager"
SETTINGS_PATH = "/org/freedesktop/NetworkManager/Settings"
SETTINGS = f"{NM}.Settings"
CONNECTION = f"{NM}.Settings.Connection"

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


async def listed(bus: MessageBus) -> list[tuple[str, Profile]]:
    """Every profile NetworkManager keeps, with its settings (secrets left out)."""
    (paths,) = await call(bus, NM, SETTINGS_PATH, SETTINGS, "ListConnections")
    found = []
    for path in paths:
        try:
            (settings,) = await call(bus, NM, path, CONNECTION, "GetSettings")
        except ConnectionError:  # gone meanwhile
            continue
        found.append((path, settings))
    return found


async def forget_made(bus: MessageBus) -> int:
    """Delete each profile the robot made; how many. ``ConnectionError`` if one cannot
    be deleted."""
    gone = 0
    for path, profile in await listed(bus):
        if made(profile):
            await call(bus, NM, path, CONNECTION, "Delete")
            gone += 1
    return gone


def ssid_of(profile: Profile) -> bytes | None:
    """The network name of a Wi-Fi profile, as bytes; ``None`` for another kind."""
    wireless = profile.get("802-11-wireless")
    if wireless and "ssid" in wireless:
        return bytes(wireless["ssid"].value)
    return None
