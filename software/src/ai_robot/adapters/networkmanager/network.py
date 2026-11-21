# SPDX-License-Identifier: MIT
"""The robot's Wi-Fi through NetworkManager, the system's network service, on D-Bus
(docs/decisions/0015-set-up.md).

NetworkManager keeps the networks it knows (its connection profiles) and their passwords,
readable by root only, and reconnects by itself. The user who runs the robot must be
allowed to manage it through polkit.

NetworkManager may restart, and numbers its objects anew as it starts: a path one gave
means nothing to the next, or another of its objects. So each operation speaks to the
NetworkManager that runs as it begins, by its unique name, as libnm sends each call to the
name's owner and fetches everything again from a new one (``priv->name_owner`` and
``_init_release_all``, nm-client.c, NetworkManager 1.54.0, read 2026-11-21). An operation
whose NetworkManager stops fails, at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterator
from typing import Any

from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus

from ai_robot.adapters.dbus import CallError, Owner, call, connect, gone, watch
from ai_robot.adapters.networkmanager import profiles
from ai_robot.adapters.networkmanager.profiles import CONNECTION, NM
from ai_robot.core.checks import positive
from ai_robot.ports.network import (
    JoinError,
    JoinFailure,
    Network,
    NetworkState,
    NetworkStatus,
    Security,
    WifiNetwork,
    shown_name,
)

log = logging.getLogger(__name__)

# NetworkManager's D-Bus API (networkmanager.dev/docs/api/latest/).
NM_PATH = "/org/freedesktop/NetworkManager"
DEVICE = f"{NM}.Device"
WIRELESS = f"{NM}.Device.Wireless"
ACCESS_POINT = f"{NM}.AccessPoint"
IP4_CONFIG = f"{NM}.IP4Config"
PROPERTIES = "org.freedesktop.DBus.Properties"
NO_OBJECT = "/"
# Its enumerations (nm-dbus-types).
DEVICE_ACTIVATED = 100
DEVICE_FAILED = 120
CONNECTING_STATES = range(40, 100)  # prepare, config, need-auth, IP config, IP check, secondaries
REASON_NO_SECRETS = 7
REASON_SUPPLICANT_DISCONNECT = 8
REASON_IP_CONFIG_UNAVAILABLE = 5
REASON_DHCP = (15, 16, 17)  # DHCP start failed, DHCP error, DHCP failed
REASON_SSID_NOT_FOUND = 53
CONNECTIVITY_NONE, CONNECTIVITY_PORTAL, CONNECTIVITY_LIMITED, CONNECTIVITY_FULL = 1, 2, 3, 4
AP_PRIVACY = 0x1
KEY_MGMT_PSK = 0x100
KEY_MGMT_802_1X = 0x200
KEY_MGMT_SAE = 0x400
KEY_MGMT_OWE = 0x800
KEY_MGMT_EAP_SUITE_B_192 = 0x2000
KEY_MGMT = {Security.WPA2: "wpa-psk", Security.WPA3: "sae"}  # for a network not in sight
SCAN_POLL = 0.5  # seconds between two looks at whether the scan has ended


def security_of(flags: int, wpa: int, rsn: int) -> Security:
    """How an access point is protected, from its flags (NM80211ApFlags,
    NM80211ApSecurityFlags)."""
    keys = wpa | rsn
    if keys & (KEY_MGMT_802_1X | KEY_MGMT_EAP_SUITE_B_192):
        return Security.OTHER
    if keys & KEY_MGMT_PSK:
        return Security.WPA2
    if keys & KEY_MGMT_SAE:
        return Security.WPA3
    if keys & KEY_MGMT_OWE or not (flags & AP_PRIVACY):
        return Security.OPEN
    return Security.OTHER  # WEP, which the robot does not join


def failure_of(reason: int) -> JoinFailure:
    """Why joining failed, from the device's state reason (NMDeviceStateReason)."""
    if reason in (REASON_NO_SECRETS, REASON_SUPPLICANT_DISCONNECT):
        return JoinFailure.WRONG_PASSWORD
    if reason == REASON_SSID_NOT_FOUND:
        return JoinFailure.NOT_FOUND
    if reason == REASON_IP_CONFIG_UNAVAILABLE or reason in REASON_DHCP:
        return JoinFailure.NO_ADDRESS
    return JoinFailure.OTHER


class NetworkManagerNetwork(Network):
    """The Wi-Fi interface ``interface`` (``wlan0``), through NetworkManager on the
    D-Bus ``bus``: ``system``, or the address of another bus, as the tests use. A scan
    may take ``scan_timeout`` seconds."""

    def __init__(self, *, interface: str, bus: str, scan_timeout: float) -> None:
        if not interface:
            raise ValueError("interface must not be empty")
        self.interface = interface
        self.bus_address = bus
        self.scan_timeout = positive("scan_timeout", scan_timeout)
        self._bus: MessageBus | None = None
        self._owner = Owner(NM)
        self._device: tuple[str, str] | None = None  # NetworkManager's unique name, the path
        self._joining: tuple[str, str] | None = None  # the same, for the join that waits
        # (state, reason) as the device changes; None once the join's NetworkManager left
        self._changes: asyncio.Queue[tuple[int, int] | None] = asyncio.Queue()

    async def open(self) -> None:
        """Connect, and find the Wi-Fi device. ``ConnectionError`` if NetworkManager does
        not run, or has no such device."""
        self._bus = await connect(self.bus_address)
        self._bus.add_message_handler(self._on_message)
        try:
            await self._owner.follow(self._bus)
            await watch(  # of every device: a new NetworkManager may give it another path
                self._bus,
                f"type='signal',sender='{NM}',interface='{DEVICE}',member='StateChanged'",
            )
            await self._reach()
        except BaseException:
            self._bus.disconnect()
            self._bus = None
            raise

    async def close(self) -> None:
        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None

    # ---- the port ----

    async def scan(self) -> list[WifiNetwork]:
        nm, device = await self._reach()
        before = await self._get(nm, device, WIRELESS, "LastScan")
        try:
            await self._call(nm, device, WIRELESS, "RequestScan", "a{sv}", [{}])
        except ConnectionError as error:  # already scanning, or scanned just now
            log.info("NetworkManager did not start a scan: %s", error)
        else:
            deadline = asyncio.get_running_loop().time() + self.scan_timeout
            while await self._get(nm, device, WIRELESS, "LastScan") == before:
                if asyncio.get_running_loop().time() >= deadline:
                    log.warning("the Wi-Fi scan took too long; showing what was seen")
                    break
                await asyncio.sleep(SCAN_POLL)
        saved = set((await self._saved(nm)).keys())
        strongest: dict[bytes, WifiNetwork] = {}
        for _, point in await self._access_points(nm, device):
            ssid = bytes(point["Ssid"].value)
            found = WifiNetwork(
                ssid,
                security_of(point["Flags"].value, point["WpaFlags"].value, point["RsnFlags"].value),
                int(point["Strength"].value),
                ssid in saved,
            )
            if ssid and (ssid not in strongest or found.signal > strongest[ssid].signal):
                strongest[ssid] = found
        return sorted(strongest.values(), key=lambda network: -network.signal)

    async def join(
        self,
        ssid: bytes,
        password: str | None,
        *,
        hidden: bool,
        security: Security | None,
        timeout: float,
    ) -> NetworkStatus:
        nm, device = await self._reach()
        saved = (await self._saved(nm)).get(ssid)
        if saved is not None and password is None:
            with self._listening(nm, device):
                (active,) = await self._call(
                    nm, NM_PATH, NM, "ActivateConnection", "ooo", [saved, device, NO_OBJECT]
                )
                await self._activate(nm, timeout, active=active, added=None)
            return await self.status()
        settings = self._settings(ssid, password, hidden=hidden, security=security)
        point = NO_OBJECT if hidden else await self._access_point(nm, device, ssid)
        if point is None:
            raise JoinError(JoinFailure.NOT_FOUND, "the network is not in reach")
        with self._listening(nm, device):
            added, active, _ = await self._call(
                nm,
                NM_PATH,
                NM,
                "AddAndActivateConnection2",
                "a{sa{sv}}ooa{sv}",
                [settings, device, point, {"persist": Variant("s", "disk")}],
            )
            await self._activate(nm, timeout, active=active, added=added)
        if saved is not None:  # the new password works: forget the old one
            await self._call(nm, saved, CONNECTION, "Delete", "", [])
        return await self.status()

    async def status(self) -> NetworkStatus:
        nm, device = await self._reach()
        state = await self._get(nm, device, DEVICE, "State")
        if state in CONNECTING_STATES:
            return NetworkStatus(NetworkState.CONNECTING)
        if state != DEVICE_ACTIVATED:
            return NetworkStatus(NetworkState.DISCONNECTED)
        point = await self._get(nm, device, WIRELESS, "ActiveAccessPoint")
        ssid = (
            bytes(await self._get(nm, point, ACCESS_POINT, "Ssid")) if point != NO_OBJECT else None
        )
        address = None
        config = await self._get(nm, device, DEVICE, "Ip4Config")
        if config != NO_OBJECT:
            addresses = await self._get(nm, config, IP4_CONFIG, "AddressData")
            if addresses:
                address = addresses[0]["address"].value
        connectivity = await self._get(nm, NM_PATH, NM, "Connectivity")
        internet = {
            CONNECTIVITY_FULL: True,
            CONNECTIVITY_NONE: False,
            CONNECTIVITY_PORTAL: False,
            CONNECTIVITY_LIMITED: False,
        }.get(connectivity)
        return NetworkStatus(NetworkState.CONNECTED, ssid, address, internet)

    # ---- joining ----

    def _settings(
        self, ssid: bytes, password: str | None, *, hidden: bool, security: Security | None
    ) -> dict[str, dict[str, Variant]]:
        """A new connection profile. NetworkManager completes what is missing from the
        access point in sight, such as how its keys are managed. The profile is named after
        the network as a person reads it (``shown_name``): a D-Bus string cannot hold the
        NUL byte that a network's name may, and NetworkManager, left to name it, would cut
        the name at that byte, and refuse a name that begins with one as empty
        (``nm_utils_ssid_to_utf8`` and ``get_new_connection_name``, 1.54, read
        2026-11-21). A name with no other character is named by its bytes, in hex."""
        settings: dict[str, dict[str, Variant]] = {
            "connection": {
                "id": Variant("s", shown_name(ssid) or ssid.hex()),
                "type": Variant("s", "802-11-wireless"),
                "autoconnect": Variant("b", True),
            },
            "802-11-wireless": {
                "ssid": Variant("ay", ssid),
                "mode": Variant("s", "infrastructure"),
                "hidden": Variant("b", hidden),
            },
        }
        wireless_security: dict[str, Variant] = {}
        if hidden and security in KEY_MGMT:
            wireless_security["key-mgmt"] = Variant("s", KEY_MGMT[security])
        if password is not None:
            wireless_security["psk"] = Variant("s", password)
        if wireless_security:
            settings["802-11-wireless-security"] = wireless_security
        profiles.mark(settings)  # the factory reset forgets it
        return settings

    @contextlib.contextmanager
    def _listening(self, nm: str, device: str) -> Iterator[None]:
        """Hear the states of ``device`` that the NetworkManager ``nm`` tells, from now
        on: a state may come before the answer to the call that activates."""
        while not self._changes.empty():
            self._changes.get_nowait()
        self._joining = (nm, device)
        try:
            yield
        finally:
            self._joining = None

    async def _activate(self, nm: str, timeout: float, *, active: str, added: str | None) -> None:
        """Wait until the device is on the network, as ``nm`` activates it. On failure,
        the profile just ``added`` is deleted. If ``nm`` stops, the join ends at once, and
        the profile stays: the next NetworkManager reads it from the disk, under another
        path, marked as the robot's; a join that works replaces it, and the factory reset
        forgets it."""
        try:
            async with asyncio.timeout(timeout):
                while True:
                    change = await self._changes.get()
                    if change is None:
                        raise ConnectionError("NetworkManager stopped while the robot joined")
                    state, reason = change
                    if state == DEVICE_ACTIVATED:
                        return
                    if state == DEVICE_FAILED:  # disconnecting from another network is not
                        raise JoinError(failure_of(reason), f"NetworkManager's reason {reason}")
        except TimeoutError:
            with contextlib.suppress(ConnectionError):
                await self._call(nm, NM_PATH, NM, "DeactivateConnection", "o", [active])
            await self._delete(nm, added)
            raise JoinError(JoinFailure.OTHER, "joining the network took too long") from None
        except JoinError:
            await self._delete(nm, added)
            raise

    async def _delete(self, nm: str, added: str | None) -> None:
        if added is not None:
            with contextlib.suppress(ConnectionError):
                await self._call(nm, added, CONNECTION, "Delete", "", [])

    def _on_message(self, message: Message) -> None:
        """NetworkManager comes or goes, or says the device's state changed. Only the
        signals of the NetworkManager that a join waits on count, as the bus says who sent
        each message (``Owner``)."""
        if self._owner.changed(message):
            if self._joining is not None:  # its NetworkManager no longer owns the name
                self._changes.put_nowait(None)
            return
        if (
            self._joining is not None
            and (message.sender, message.path) == self._joining
            and message.message_type is MessageType.SIGNAL
            and message.interface == DEVICE
            and message.member == "StateChanged"
        ):
            new, _old, reason = message.body
            self._changes.put_nowait((new, reason))

    # ---- what NetworkManager knows ----

    async def _reach(self) -> tuple[str, str]:
        """The NetworkManager that runs now, by its unique name, and the Wi-Fi device's
        path as it knows it."""
        nm = self._owner.unique
        if nm is None:
            raise ConnectionError("NetworkManager does not run")
        if self._device is None or self._device[0] != nm:
            (path,) = await self._call(nm, NM_PATH, NM, "GetDeviceByIpIface", "s", [self.interface])
            self._device = (nm, path)
        return self._device

    async def _saved(self, nm: str) -> dict[bytes, str]:
        """The Wi-Fi networks ``nm`` has a profile for: name -> profile."""
        assert self._bus is not None, "the network is not open"
        saved: dict[bytes, str] = {}
        for path, profile in await profiles.listed(self._bus, nm):
            ssid = profiles.ssid_of(profile)
            if ssid is not None:
                saved[ssid] = path
        return saved

    async def _access_points(self, nm: str, device: str) -> list[tuple[str, dict[str, Variant]]]:
        """The access points ``device`` sees, each with its properties. It passes over one
        that went out of sight as it asked; ``ConnectionError`` for any other failure, such
        as ``nm`` that stops, so that a part of them is never taken for all of them."""
        (paths,) = await self._call(nm, device, WIRELESS, "GetAllAccessPoints", "", [])
        points = []
        for path in paths:
            try:
                (point,) = await self._call(nm, path, PROPERTIES, "GetAll", "s", [ACCESS_POINT])
            except CallError as error:
                if gone(error):
                    continue
                raise
            points.append((path, point))
        return points

    async def _access_point(self, nm: str, device: str, ssid: bytes) -> str | None:
        """The strongest access point of ``ssid`` in sight, if any."""
        best: tuple[int, str] | None = None
        for path, point in await self._access_points(nm, device):
            if bytes(point["Ssid"].value) == ssid:
                strength = int(point["Strength"].value)
                if best is None or strength > best[0]:
                    best = (strength, path)
        return None if best is None else best[1]

    async def _get(self, nm: str, path: str, interface: str, name: str) -> Any:
        (value,) = await self._call(nm, path, PROPERTIES, "Get", "ss", [interface, name])
        return value.value

    async def _call(
        self, nm: str, path: str, interface: str, member: str, signature: str, body: list[Any]
    ) -> list[Any]:
        """Call the NetworkManager ``nm``, by its unique name: never another."""
        assert self._bus is not None, "the network is not open"
        return await call(self._bus, nm, path, interface, member, signature, body)
