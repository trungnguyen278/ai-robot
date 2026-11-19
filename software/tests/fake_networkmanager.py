# SPDX-License-Identifier: MIT
"""NetworkManager, as the network adapter's tests need it (networkmanager.dev/docs/api):
on a private D-Bus, one Wi-Fi device, the access points it sees, the connection profiles
it keeps, and joins that succeed or fail as a real one does, state by state."""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass
from typing import Any

from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus

NM = "org.freedesktop.NetworkManager"
NM_PATH = "/org/freedesktop/NetworkManager"
DEVICE_PATH = f"{NM_PATH}/Devices/3"
SETTINGS_PATH = f"{NM_PATH}/Settings"
IP4_PATH = f"{NM_PATH}/IP4Config/1"
PROPERTIES = "org.freedesktop.DBus.Properties"
ADDRESS = "192.168.1.23"

# Its enumerations (nm-dbus-types).
PREPARE, CONFIG, NEED_AUTH, IP_CONFIG, ACTIVATED, FAILED, DISCONNECTED = (
    40,
    50,
    60,
    70,
    100,
    120,
    30,
)
NO_SECRETS, DHCP_FAILED, SSID_NOT_FOUND = 7, 17, 53


@dataclass
class AccessPoint:
    ssid: bytes
    strength: int
    flags: int = 0  # NM80211ApFlags
    wpa: int = 0  # NM80211ApSecurityFlags
    rsn: int = 0
    password: str | None = None  # what it takes, if protected
    hidden: bool = False  # announces no name
    dhcp: bool = True  # whether it gives an address


class FakeNetworkManager:
    """Owns ``org.freedesktop.NetworkManager`` on the bus at ``address``, with the device
    ``wlan0`` seeing ``points``. ``profiles`` holds the connection profiles (path ->
    settings, secrets included); ``stall`` makes joins hang; ``calls`` lists the methods
    called."""

    def __init__(self, address: str, points: list[AccessPoint]) -> None:
        self.address = address
        self.points = {f"{NM_PATH}/AccessPoint/{n}": p for n, p in enumerate(points, 1)}
        self.profiles: dict[str, dict[str, dict[str, Any]]] = {}
        self.state = DISCONNECTED
        self.active_point = "/"
        self.last_scan = 1000
        self.stall = False
        self.calls: list[str] = []
        self.bus: MessageBus | None = None
        self._numbers = itertools.count(1)
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        self.bus = await MessageBus(bus_address=self.address).connect()
        await self.bus.request_name(NM)
        self.bus.add_message_handler(self._handle)

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self.bus is not None:
            self.bus.disconnect()

    def save(self, ssid: bytes, password: str | None, user: dict[str, str] | None = None) -> str:
        """Keep a profile for ``ssid``, as one saved earlier, with ``user`` data if any."""
        path = f"{SETTINGS_PATH}/{next(self._numbers)}"
        security = {} if password is None else {"psk": password}
        self.profiles[path] = {
            "connection": {"id": ssid.decode(errors="replace"), "type": "802-11-wireless"},
            "802-11-wireless": {"ssid": ssid},
            **({"802-11-wireless-security": security} if security else {}),
            **({"user": {"data": user}} if user is not None else {}),
        }
        return path

    # ---- D-Bus ----

    def _handle(self, message: Message) -> bool | None:
        if message.message_type is not MessageType.METHOD_CALL:
            return None
        self.calls.append(message.member)
        try:
            answer = self._answer(message)
        except LookupError as error:
            self._send(Message.new_error(message, f"{NM}.UnknownObject", str(error)))
            return True
        if answer is None:
            return None
        signature, body = answer
        self._send(Message.new_method_return(message, signature, body))
        return True

    def _answer(self, message: Message) -> tuple[str, list[Any]] | None:
        member, body = message.member, message.body
        if message.interface == PROPERTIES:
            values = self._properties(message.path, body[0])
            if member == "GetAll":
                return "a{sv}", [values]
            if member == "Get":
                return "v", [values[body[1]]]
            return None
        if member == "GetDeviceByIpIface":
            if body[0] != "wlan0":
                raise LookupError(f"no device {body[0]}")
            return "o", [DEVICE_PATH]
        if member == "RequestScan":
            self.last_scan += 1
            return "", []
        if member == "GetAllAccessPoints":
            return "ao", [list(self.points)]
        if member == "ListConnections":
            return "ao", [list(self.profiles)]
        if member == "GetSettings":
            settings = self.profiles[message.path]
            public = {name: dict(group) for name, group in settings.items()}
            public.get("802-11-wireless-security", {}).pop("psk", None)  # secrets stay
            return "a{sa{sv}}", [self._variants(public)]
        if member == "Delete":
            del self.profiles[message.path]
            return "", []
        if member == "AddAndActivateConnection2":
            settings, _device, point, _options = body
            path = f"{SETTINGS_PATH}/{next(self._numbers)}"
            self.profiles[path] = {
                name: {key: value.value for key, value in group.items()}
                for name, group in settings.items()
            }
            self._spawn(self._activate(path, point))
            return "ooa{sv}", [path, f"{NM_PATH}/ActiveConnection/1", {}]
        if member == "ActivateConnection":
            self._spawn(self._activate(body[0], body[2]))
            return "o", [f"{NM_PATH}/ActiveConnection/1"]
        if member == "DeactivateConnection":
            self.state = DISCONNECTED
            return "", []
        return None

    def _properties(self, path: str, interface: str) -> dict[str, Variant]:
        if path == NM_PATH:
            return {"Connectivity": Variant("u", 4)}
        if path == DEVICE_PATH and interface.endswith("Device"):
            config = IP4_PATH if self.state == ACTIVATED else "/"
            return {"State": Variant("u", self.state), "Ip4Config": Variant("o", config)}
        if path == DEVICE_PATH:
            return {
                "LastScan": Variant("x", self.last_scan),
                "ActiveAccessPoint": Variant("o", self.active_point),
            }
        if path == IP4_PATH:
            address = {"address": Variant("s", ADDRESS), "prefix": Variant("u", 24)}
            return {"AddressData": Variant("aa{sv}", [address])}
        point = self.points.get(path)
        if point is None:
            raise LookupError(path)
        return {
            "Ssid": Variant("ay", b"" if point.hidden else point.ssid),
            "Strength": Variant("y", point.strength),
            "Flags": Variant("u", point.flags),
            "WpaFlags": Variant("u", point.wpa),
            "RsnFlags": Variant("u", point.rsn),
        }

    async def _activate(self, profile: str, point_path: str) -> None:
        """Join as NetworkManager does, telling each state of the device."""
        settings = self.profiles[profile]
        ssid = settings["802-11-wireless"]["ssid"]
        password = settings.get("802-11-wireless-security", {}).get("psk")
        found = next(((path, p) for path, p in self.points.items() if p.ssid == ssid), (None, None))
        for state in (PREPARE, CONFIG):
            await self._state(state, 0)
        if self.stall:
            return
        path, point = found
        if point is None:
            await self._state(FAILED, SSID_NOT_FOUND)
            await self._state(DISCONNECTED, SSID_NOT_FOUND)
            return
        if point.password is not None and password != point.password:
            await self._state(NEED_AUTH, 0)
            await self._state(FAILED, NO_SECRETS)
            await self._state(DISCONNECTED, NO_SECRETS)
            return
        await self._state(IP_CONFIG, 0)
        if not point.dhcp:
            await self._state(FAILED, DHCP_FAILED)
            await self._state(DISCONNECTED, DHCP_FAILED)
            return
        self.active_point = path
        await self._state(ACTIVATED, 0)

    async def _state(self, state: int, reason: int) -> None:
        old, self.state = self.state, state
        self._send(
            Message.new_signal(
                DEVICE_PATH, f"{NM}.Device", "StateChanged", "uuu", [state, old, reason]
            )
        )
        await asyncio.sleep(0.01)

    @staticmethod
    def _variants(settings: dict[str, dict[str, Any]]) -> dict[str, dict[str, Variant]]:
        kinds = {bytes: "ay", str: "s", bool: "b", int: "u", dict: "a{ss}"}
        return {
            name: {key: Variant(kinds[type(value)], value) for key, value in group.items()}
            for name, group in settings.items()
        }

    def _send(self, message: Message) -> None:
        assert self.bus is not None
        self.bus.send(message)

    def _spawn(self, coroutine: Any) -> None:
        task = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
