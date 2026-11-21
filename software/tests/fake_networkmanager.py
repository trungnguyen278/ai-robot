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
# Its answers to a call that fails, as NetworkManager 1.54.0 gives them (read 2026-11-21):
# GDBus's to an object not there (gio/gdbusconnection.c, GLib 2.86.0), its own to a call
# while it stops (nm-dbus-manager.c) and to a profile another user keeps private
# (nm-settings-connection.c), and the name of NM_MANAGER_ERROR_UNKNOWN_DEVICE (nm-errors.c).
NO_SUCH_OBJECT = "org.freedesktop.DBus.Error.UnknownMethod"
EXITING = "org.freedesktop.DBus.Error.Failed"
PRIVATE = f"{NM}.Settings.PermissionDenied"
UNKNOWN_DEVICE = f"{NM}.UnknownDevice"

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


class Refused(Exception):
    """A call the fake answers with the D-Bus error ``name``."""

    def __init__(self, name: str, text: str) -> None:
        super().__init__(text)
        self.name = name


class FakeNetworkManager:
    """Owns ``org.freedesktop.NetworkManager`` on the bus at ``address``, with the device
    ``wlan0``, at ``device``, seeing ``points``; if another owns the name, it waits for it,
    as the bus queues a request (the D-Bus specification, ``RequestName``). ``profiles``
    holds the connection profiles (path -> settings, secrets included), and ``private``
    those another user keeps to themselves; ``ghosts`` are the paths of profiles and
    access points it lists but no longer has, as one gone as it answers. ``stall`` makes
    joins hang; ``leave_after`` names a method after whose answer it leaves the bus, and
    ``exit_after`` one after whose answer it fails every call, as it does while it stops;
    ``calls`` lists the methods called, and ``destinations`` the names each call was sent
    to."""

    def __init__(self, address: str, points: list[AccessPoint], device: str = DEVICE_PATH) -> None:
        self.address = address
        self.device = device
        self.points = {f"{NM_PATH}/AccessPoint/{n}": p for n, p in enumerate(points, 1)}
        self.profiles: dict[str, dict[str, dict[str, Any]]] = {}
        self.state = DISCONNECTED
        self.active_point = "/"
        self.last_scan = 1000
        self.stall = False
        self.leave_after: str | None = None
        self.exit_after: str | None = None
        self.private: set[str] = set()
        self.ghosts: set[str] = set()
        self._exiting = False
        self.calls: list[str] = []
        self.destinations: list[str] = []
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
            self.bus = None

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
        self.destinations.append(message.destination)
        try:
            if self._exiting:
                raise Refused(EXITING, "NetworkManager is exiting")
            answer = self._answer(message)
        except Refused as error:
            self._send(Message.new_error(message, error.name, str(error)))
            return True
        if answer is None:
            return None
        signature, body = answer
        sent = self._send(Message.new_method_return(message, signature, body))
        if message.member == self.leave_after:
            self._spawn(self._leave(sent))
        if message.member == self.exit_after:
            self._exiting = True
        return True

    async def _leave(self, sent: asyncio.Future[None]) -> None:
        await sent
        await self.stop()

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
                raise Refused(UNKNOWN_DEVICE, "No device found for the requested iface.")
            return "o", [self.device]
        if member == "RequestScan":
            self.last_scan += 1
            return "", []
        if member == "GetAllAccessPoints":
            return "ao", [[*self._ghosts(f"{NM_PATH}/AccessPoint/"), *self.points]]
        if member == "ListConnections":
            return "ao", [[*self._ghosts(f"{SETTINGS_PATH}/"), *self.profiles]]
        if member == "GetSettings":
            if message.path in self.private:
                raise Refused(PRIVATE, "Insufficient privileges")
            settings = self._profile(message.path)
            public = {name: dict(group) for name, group in settings.items()}
            public.get("802-11-wireless-security", {}).pop("psk", None)  # secrets stay
            return "a{sa{sv}}", [self._variants(public)]
        if member == "Delete":
            self._profile(message.path)
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
        if path == self.device and interface.endswith("Device"):
            config = IP4_PATH if self.state == ACTIVATED else "/"
            return {"State": Variant("u", self.state), "Ip4Config": Variant("o", config)}
        if path == self.device:
            return {
                "LastScan": Variant("x", self.last_scan),
                "ActiveAccessPoint": Variant("o", self.active_point),
            }
        if path == IP4_PATH:
            address = {"address": Variant("s", ADDRESS), "prefix": Variant("u", 24)}
            return {"AddressData": Variant("aa{sv}", [address])}
        point = self.points.get(path)
        if point is None:
            raise _no_such_object(path)
        return {
            "Ssid": Variant("ay", b"" if point.hidden else point.ssid),
            "Strength": Variant("y", point.strength),
            "Flags": Variant("u", point.flags),
            "WpaFlags": Variant("u", point.wpa),
            "RsnFlags": Variant("u", point.rsn),
        }

    def _profile(self, path: str) -> dict[str, dict[str, Any]]:
        settings = self.profiles.get(path)
        if settings is None:
            raise _no_such_object(path)
        return settings

    def _ghosts(self, prefix: str) -> list[str]:
        return sorted(path for path in self.ghosts if path.startswith(prefix))

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
                self.device, f"{NM}.Device", "StateChanged", "uuu", [state, old, reason]
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

    def _send(self, message: Message) -> asyncio.Future[None]:
        assert self.bus is not None
        return self.bus.send(message)

    def _spawn(self, coroutine: Any) -> None:
        task = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def _no_such_object(path: str) -> Refused:
    return Refused(NO_SUCH_OBJECT, f'Object does not exist at path "{path}"')
