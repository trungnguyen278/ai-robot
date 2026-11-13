# SPDX-License-Identifier: MIT
"""The robot's Wi-Fi through NetworkManager, the system's network service, on D-Bus
(docs/decisions/0015-set-up.md).

NetworkManager keeps the networks it knows (its connection profiles) and their passwords,
readable by root only, and reconnects by itself. The user who runs the robot must be
allowed to manage it through polkit.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus

from ai_robot.adapters.dbus import Owner, call, connect, watch
from ai_robot.core.checks import positive
from ai_robot.ports.network import (
    JoinError,
    JoinFailure,
    Network,
    NetworkState,
    NetworkStatus,
    Security,
    WifiNetwork,
)

log = logging.getLogger(__name__)

# NetworkManager's D-Bus API (networkmanager.dev/docs/api/latest/).
NM = "org.freedesktop.NetworkManager"
NM_PATH = "/org/freedesktop/NetworkManager"
SETTINGS_PATH = "/org/freedesktop/NetworkManager/Settings"
DEVICE = f"{NM}.Device"
WIRELESS = f"{NM}.Device.Wireless"
ACCESS_POINT = f"{NM}.AccessPoint"
IP4_CONFIG = f"{NM}.IP4Config"
SETTINGS = f"{NM}.Settings"
CONNECTION = f"{NM}.Settings.Connection"
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
        self._device = ""
        self._changes: asyncio.Queue[tuple[int, int]] = asyncio.Queue()  # (state, reason)

    async def open(self) -> None:
        self._bus = await connect(self.bus_address)
        self._bus.add_message_handler(self._on_message)
        try:
            await self._owner.follow(self._bus)
            (self._device,) = await self._call(
                NM_PATH, NM, "GetDeviceByIpIface", "s", [self.interface]
            )
            await watch(
                self._bus,
                f"type='signal',sender='{NM}',interface='{DEVICE}',member='StateChanged',"
                f"path='{self._device}'",
            )
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
        before = await self._get(self._device, WIRELESS, "LastScan")
        try:
            await self._call(self._device, WIRELESS, "RequestScan", "a{sv}", [{}])
        except ConnectionError as error:  # already scanning, or scanned just now
            log.info("NetworkManager did not start a scan: %s", error)
        else:
            deadline = asyncio.get_running_loop().time() + self.scan_timeout
            while await self._get(self._device, WIRELESS, "LastScan") == before:
                if asyncio.get_running_loop().time() >= deadline:
                    log.warning("the Wi-Fi scan took too long; showing what was seen")
                    break
                await asyncio.sleep(SCAN_POLL)
        saved = set((await self._saved()).keys())
        strongest: dict[bytes, WifiNetwork] = {}
        for point in await self._access_points():
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
        saved = (await self._saved()).get(ssid)
        if saved is not None and password is None:
            await self._activate(saved, timeout, added=None)
            return await self.status()
        settings = self._settings(ssid, password, hidden=hidden, security=security)
        point = NO_OBJECT if hidden else await self._access_point(ssid)
        if point is None:
            raise JoinError(JoinFailure.NOT_FOUND, "the network is not in reach")
        self._drain()
        added, active, _ = await self._call(
            NM_PATH,
            NM,
            "AddAndActivateConnection2",
            "a{sa{sv}}ooa{sv}",
            [settings, self._device, point, {"persist": Variant("s", "disk")}],
        )
        await self._activate(None, timeout, added=added, active=active)
        if saved is not None:  # the new password works: forget the old one
            await self._call(saved, CONNECTION, "Delete", "", [])
        return await self.status()

    async def status(self) -> NetworkStatus:
        state = await self._get(self._device, DEVICE, "State")
        if state in CONNECTING_STATES:
            return NetworkStatus(NetworkState.CONNECTING)
        if state != DEVICE_ACTIVATED:
            return NetworkStatus(NetworkState.DISCONNECTED)
        point = await self._get(self._device, WIRELESS, "ActiveAccessPoint")
        ssid = bytes(await self._get(point, ACCESS_POINT, "Ssid")) if point != NO_OBJECT else None
        address = None
        config = await self._get(self._device, DEVICE, "Ip4Config")
        if config != NO_OBJECT:
            addresses = await self._get(config, IP4_CONFIG, "AddressData")
            if addresses:
                address = addresses[0]["address"].value
        connectivity = await self._get(NM_PATH, NM, "Connectivity")
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
        access point in sight, such as how its keys are managed."""
        settings: dict[str, dict[str, Variant]] = {
            "connection": {
                "id": Variant("s", ssid.decode("utf-8", errors="replace")),
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
        return settings

    async def _activate(
        self,
        saved: str | None,
        timeout: float,
        *,
        added: str | None,
        active: str | None = None,
    ) -> None:
        """Activate ``saved``, or wait for the profile just ``added``, until the device is
        on the network. On failure, the profile just added is deleted."""
        if saved is not None:
            self._drain()
            (active,) = await self._call(
                NM_PATH, NM, "ActivateConnection", "ooo", [saved, self._device, NO_OBJECT]
            )
        try:
            async with asyncio.timeout(timeout):
                while True:
                    state, reason = await self._changes.get()
                    if state == DEVICE_ACTIVATED:
                        return
                    if state == DEVICE_FAILED:  # disconnecting from another network is not
                        raise JoinError(failure_of(reason), f"NetworkManager's reason {reason}")
        except TimeoutError:
            if active is not None:
                with contextlib.suppress(ConnectionError):
                    await self._call(NM_PATH, NM, "DeactivateConnection", "o", [active])
            if added is not None:
                with contextlib.suppress(ConnectionError):
                    await self._call(added, CONNECTION, "Delete", "", [])
            raise JoinError(JoinFailure.OTHER, "joining the network took too long") from None
        except JoinError:
            if added is not None:
                with contextlib.suppress(ConnectionError):
                    await self._call(added, CONNECTION, "Delete", "", [])
            raise

    def _drain(self) -> None:
        while not self._changes.empty():
            self._changes.get_nowait()

    def _on_message(self, message: Message) -> None:
        """NetworkManager says the device's state changed. Only its own signals count, as
        the bus says who sent each (``Owner``)."""
        if self._owner.changed(message) or not self._owner.sent(message):
            return
        if (
            message.message_type is MessageType.SIGNAL
            and message.interface == DEVICE
            and message.member == "StateChanged"
            and message.path == self._device
        ):
            new, _old, reason = message.body
            self._changes.put_nowait((new, reason))

    # ---- what NetworkManager knows ----

    async def _saved(self) -> dict[bytes, str]:
        """The Wi-Fi networks NetworkManager has a profile for: name -> profile."""
        (paths,) = await self._call(SETTINGS_PATH, SETTINGS, "ListConnections", "", [])
        saved: dict[bytes, str] = {}
        for path in paths:
            try:
                (settings,) = await self._call(path, CONNECTION, "GetSettings", "", [])
            except ConnectionError:  # gone meanwhile
                continue
            wireless = settings.get("802-11-wireless")
            if wireless and "ssid" in wireless:
                saved[bytes(wireless["ssid"].value)] = path
        return saved

    async def _access_points(self) -> list[dict[str, Variant]]:
        (paths,) = await self._call(self._device, WIRELESS, "GetAllAccessPoints", "", [])
        points = []
        for path in paths:
            with contextlib.suppress(ConnectionError):  # gone meanwhile
                (point,) = await self._call(path, PROPERTIES, "GetAll", "s", [ACCESS_POINT])
                points.append(point)
        return points

    async def _access_point(self, ssid: bytes) -> str | None:
        """The strongest access point of ``ssid`` in sight, if any."""
        (paths,) = await self._call(self._device, WIRELESS, "GetAllAccessPoints", "", [])
        best: tuple[int, str] | None = None
        for path in paths:
            with contextlib.suppress(ConnectionError):
                (point,) = await self._call(path, PROPERTIES, "GetAll", "s", [ACCESS_POINT])
                if bytes(point["Ssid"].value) == ssid:
                    strength = int(point["Strength"].value)
                    if best is None or strength > best[0]:
                        best = (strength, path)
        return None if best is None else best[1]

    async def _get(self, path: str, interface: str, name: str) -> Any:
        (value,) = await self._call(path, PROPERTIES, "Get", "ss", [interface, name])
        return value.value

    async def _call(
        self, path: str, interface: str, member: str, signature: str, body: list[Any]
    ) -> list[Any]:
        assert self._bus is not None, "the network is not open"
        return await call(self._bus, NM, path, interface, member, signature, body)
