# SPDX-License-Identifier: MIT
"""BlueZ, as the set-up link's tests need it (BlueZ 5.85, doc/org.bluez.*.rst and
src/gatt-database.c): on a private D-Bus, a controller that takes the robot's GATT
application and advertisement, and phones that connect, write fragments, and enable and
confirm indications, as BlueZ relays them.

A phone that enables indications has BlueZ call ``AcquireNotify``, which hands BlueZ one
end of a socket pair: BlueZ indicates each packet read from it, of 512 bytes at most, to
that phone, and writes one byte, 0x01, for each confirmation (``sock_io_read`` and
``sock_io_write``). It closes its end when the phone disables indications or disconnects.

BlueZ offers its managers on the controller's object, and says when they come and go
(``InterfacesAdded`` and ``InterfacesRemoved`` from its object manager, at its root). The
advertising manager comes once the controller has told its features
(``read_adv_features_callback``, src/advertising.c). BlueZ forgets every registration when
it stops, or when the controller goes away (``adapter_remove``, src/adapter.c).
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import os
import socket
from typing import Any

from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus

from ai_robot.adapters.bluez.fragments import Joiner, split

BLUEZ = "org.bluez"
ADAPTER = "/org/bluez/hci0"
GATT_MANAGER = "org.bluez.GattManager1"
ADVERTISING_MANAGER = "org.bluez.LEAdvertisingManager1"
MANAGERS = ("org.bluez.Adapter1", GATT_MANAGER, ADVERTISING_MANAGER)
OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"
DEVICE = "org.bluez.Device1"
CHARACTERISTIC = "org.bluez.GattCharacteristic1"
PROPERTIES = "org.freedesktop.DBus.Properties"
ANSWER_UUID = "eaf35dcd-e774-4881-b0c6-4990127a1289"  # the protocol's Answer
READ_BYTES = 512  # what BlueZ reads of a packet: its buffer in sock_io_read
CONFIRMATION = bytes([1])  # what BlueZ writes for each confirmation (sock_io_write)
DEFAULT_MTU = 23


def plain(value: Any) -> Any:
    """``value`` with its variants unwrapped."""
    if isinstance(value, Variant):
        return plain(value.value)
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [plain(item) for item in value]
    return value


class FakeBluez:
    """Owns ``org.bluez`` on the bus at ``address``.

    ``application`` holds the robot's GATT objects once it registers them (path ->
    interface -> properties), and ``advertisement`` the properties of its advertisement
    while it advertises. ``refuse`` makes the next registration fail. Phones are named
    by their device's object path; those in ``silent`` do not confirm indications.
    ``managers`` holds the interfaces the controller's object offers. ``late`` names
    interfaces that come as BlueZ lists its objects, after the listing; ``hold`` keeps
    listings unanswered; ``vanish`` names interfaces that go away as BlueZ answers the
    next registration, which BlueZ then loses if it needed them. ``registrations`` tells
    each registration and unregistration, in order.
    """

    def __init__(self, address: str) -> None:
        self.address = address
        self.bus: MessageBus | None = None
        self.managers: set[str] = set()
        self.application: dict[str, dict[str, dict[str, Any]]] = {}
        self.advertisement: dict[str, Any] | None = None
        self.robot = ""  # the robot's unique name on the bus
        self.disconnected: list[str] = []  # devices the robot asked to disconnect
        self.refuse = False
        self.refuse_advertisement = False
        self.late: set[str] = set()
        self.hold = False
        self.held: list[Message] = []  # listings not answered
        self.vanish: tuple[str, ...] = ()
        self.registrations: list[str] = []
        self.refuse_all = False  # every call to the controller fails
        self.silent: set[str] = set()
        # Each phone's indications, and the channels the robot closed, in order.
        self.indications: collections.defaultdict[str, asyncio.Queue[bytes]] = (
            collections.defaultdict(asyncio.Queue)
        )
        self.closed: list[str] = []
        self.acquired: list[bool] = []  # NotifyAcquired, each time the robot says it changed
        self.errors: list[str] = []  # the errors the robot sent
        self._channels: dict[str, socket.socket] = {}  # each phone's channel, BlueZ's end
        self._shut: set[socket.socket] = set()  # the ends BlueZ shut down
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self, offers: tuple[str, ...] = MANAGERS) -> None:
        """Start, on a connection of its own: again after ``stop``, as a BlueZ that
        restarts, which knows nothing of before."""
        self.application, self.advertisement, self.robot = {}, None, ""
        self.bus = await MessageBus(bus_address=self.address, negotiate_unix_fd=True).connect()
        self.bus.add_message_handler(self._handle)
        await self.bus.request_name(BLUEZ)
        if offers:
            self.add(*offers)

    async def stop(self) -> None:
        """Stop, as bluetoothd exits: its channels close, and it leaves the bus."""
        for task in list(self._tasks):
            task.cancel()
        self._channels.clear()
        self.managers.clear()
        if self.bus is not None:
            self.bus.disconnect()
            await self.bus.wait_for_disconnect()

    def add(self, *interfaces: str) -> None:
        """The controller's object offers ``interfaces``."""
        assert self.bus is not None
        self.managers.update(interfaces)
        self.bus.send(
            Message.new_signal(
                "/",
                OBJECT_MANAGER,
                "InterfacesAdded",
                "oa{sa{sv}}",
                [ADAPTER, {interface: {} for interface in interfaces}],
            )
        )

    def remove(self, *interfaces: str) -> None:
        """The controller's object no longer offers ``interfaces``: all of them when the
        controller goes away, and what was registered with them is forgotten."""
        assert self.bus is not None
        self.managers.difference_update(interfaces)
        if GATT_MANAGER in interfaces:
            self.application = {}
            for device in list(self._channels):
                self.unsubscribe(device)
        if ADVERTISING_MANAGER in interfaces:
            self.advertisement = None
        self.bus.send(
            Message.new_signal(
                "/", OBJECT_MANAGER, "InterfacesRemoved", "oas", [ADAPTER, list(interfaces)]
            )
        )

    # ---- what the robot asks of BlueZ ----

    def _handle(self, message: Message) -> bool | None:
        if message.message_type is MessageType.ERROR:
            self.errors.append(message.error_name or "")
            return None
        if message.message_type is MessageType.SIGNAL and message.member == "PropertiesChanged":
            interface, changed, _ = message.body
            if interface == CHARACTERISTIC and "NotifyAcquired" in changed:
                self.acquired.append(changed["NotifyAcquired"].value)
            return None
        if message.message_type is not MessageType.METHOD_CALL:
            return None
        call = (message.interface, message.member)
        if self.refuse_all and message.path.startswith(ADAPTER):
            self._error(message, "org.bluez.Error.Failed", "refused for the test")
        elif message.interface in MANAGERS and (
            message.path != ADAPTER or message.interface not in self.managers
        ):
            self._error(message, "org.freedesktop.DBus.Error.UnknownObject", "no such object")
        elif call == (OBJECT_MANAGER, "GetManagedObjects") and message.path == "/":
            if self.hold:
                self.held.append(message)
                return True
            objects = {ADAPTER: {name: {} for name in self.managers}} if self.managers else {}
            if self.late:
                self.add(*self.late)
                self.late = set()
            self.bus.send(Message.new_method_return(message, "a{oa{sa{sv}}}", [objects]))
        elif (call == (GATT_MANAGER, "RegisterApplication") and self.application) or (
            call == (ADVERTISING_MANAGER, "RegisterAdvertisement") and self.advertisement
        ):
            self._error(message, "org.bluez.Error.AlreadyExists", "registered already")
        elif call == (GATT_MANAGER, "RegisterApplication"):
            self._spawn(self._register_application(message))
        elif call == (GATT_MANAGER, "UnregisterApplication"):
            self.registrations.append("-application")
            self.application = {}
            self._reply(message)
        elif call == (ADVERTISING_MANAGER, "RegisterAdvertisement"):
            self._spawn(self._register_advertisement(message))
        elif call == (ADVERTISING_MANAGER, "UnregisterAdvertisement"):
            self.registrations.append("-advertisement")
            self.advertisement = None
            self._reply(message)
        elif call == (DEVICE, "Disconnect"):
            self.disconnected.append(message.path)
            self._reply(message)
            self.disconnect(message.path)
        else:
            return None
        return True

    async def _register_application(self, message: Message) -> None:
        if self.refuse:
            self.refuse = False
            self._error(message, "org.bluez.Error.Failed", "refused for the test")
            return
        self.robot = message.sender
        (objects,) = (
            await self.call(
                message.body[0], "org.freedesktop.DBus.ObjectManager", "GetManagedObjects", "", []
            )
        ).body
        if self._vanished(GATT_MANAGER):
            self._reply(message)
            return
        self.application = plain(objects)
        self.registrations.append("application")
        self._reply(message)
        await self.call(
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "AddMatch",
            "s",
            [f"type='signal',sender='{self.robot}',member='PropertiesChanged'"],
            destination="org.freedesktop.DBus",
        )

    async def _register_advertisement(self, message: Message) -> None:
        (properties,) = (
            await self.call(
                message.body[0],
                PROPERTIES,
                "GetAll",
                "s",
                ["org.bluez.LEAdvertisement1"],
                destination=message.sender,
            )
        ).body
        if self.refuse_advertisement:
            self.refuse_advertisement = False
            self._error(message, "org.bluez.Error.Failed", "refused for the test")
            return
        if self._vanished(ADVERTISING_MANAGER):
            self._reply(message)
            return
        self.advertisement = plain(properties)
        self.registrations.append("advertisement")
        self._reply(message)

    def _vanished(self, manager: str) -> bool:
        """The interfaces ``vanish`` names go, as BlueZ answers; whether ``manager``
        did."""
        vanish, self.vanish = self.vanish, ()
        if vanish:
            self.remove(*vanish)
        return manager in vanish

    # ---- phones ----

    def characteristic(self, uuid: str) -> str:
        """The path of the robot's characteristic ``uuid``."""
        for path, interfaces in self.application.items():
            if interfaces.get(CHARACTERISTIC, {}).get("UUID") == uuid:
                return path
        raise KeyError(uuid)

    async def subscribe(self, device: str, mtu: int = DEFAULT_MTU, *, again: bool = False) -> None:
        """``device`` enables the answer's indications: BlueZ asks the robot for a channel,
        with the options BlueZ gives (``append_options``). BlueZ asks again only once the
        channel it holds has closed (``ccc_write_cb``), unless ``again``."""
        assert again or device not in self._channels, "subscribed already"
        reply = await self.call(
            self.characteristic(ANSWER_UUID),
            CHARACTERISTIC,
            "AcquireNotify",
            "a{sv}",
            [
                {
                    "device": Variant("o", device),
                    "link": Variant("s", "LE"),
                    "mtu": Variant("q", mtu),
                }
            ],
        )
        index, given = reply.body
        assert reply.signature == "hq" and given == mtu, reply.body
        descriptor = reply.unix_fds[index]
        assert not os.get_blocking(descriptor)  # as bluetoothctl makes it (client/gatt.c)
        end = socket.socket(fileno=descriptor)
        end.setblocking(False)  # as Python's socket takes it
        assert end.type == socket.SOCK_SEQPACKET
        self._channels[device] = end
        self._spawn(self._relay(device, end))

    def unsubscribe(self, device: str) -> None:
        """``device`` disables the answer's indications: BlueZ shuts its end down at once
        (``att_disconnect_cb``), and closes it."""
        if (end := self._channels.pop(device, None)) is not None:
            self._shut.add(end)
            with contextlib.suppress(OSError):  # closed already
                end.shutdown(socket.SHUT_RDWR)

    async def write(self, device: str, uuid: str, fragment: bytes, mtu: int) -> None:
        await self.call(
            self.characteristic(uuid),
            CHARACTERISTIC,
            "WriteValue",
            "aya{sv}",
            [fragment, {"device": Variant("o", device), "mtu": Variant("q", mtu)}],
        )

    async def send(self, device: str, uuid: str, message: bytes, mtu: int) -> None:
        """``device`` writes ``message``, in fragments, to characteristic ``uuid``."""
        for fragment in split(message, mtu):
            await self.write(device, uuid, fragment, mtu)

    async def receive(self, device: str, timeout: float) -> bytes:
        """The next whole message the robot indicated to ``device``."""
        joiner = Joiner()
        while True:
            fragment = await asyncio.wait_for(self.indications[device].get(), timeout)
            message = joiner.add(fragment)
            if message is not None:
                return message

    def disconnect(self, device: str) -> None:
        """``device`` disconnects: BlueZ closes its channel and says so."""
        assert self.bus is not None
        self.unsubscribe(device)
        self.bus.send(
            Message.new_signal(
                device,
                PROPERTIES,
                "PropertiesChanged",
                "sa{sv}as",
                [DEVICE, {"Connected": Variant("b", False)}, []],
            )
        )

    async def _relay(self, device: str, end: socket.socket) -> None:
        """BlueZ's end of the channel of ``device``."""
        loop = asyncio.get_running_loop()
        with end, contextlib.suppress(OSError):
            while packet := await loop.sock_recv(end, READ_BYTES):
                self.indications[device].put_nowait(packet)
                if device not in self.silent:
                    await loop.sock_sendall(end, CONFIRMATION)
        if end not in self._shut:  # the robot closed it
            if self._channels.get(device) is end:
                del self._channels[device]
            self.closed.append(device)

    # ---- plumbing ----

    def _spawn(self, coroutine: Any) -> None:
        task = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _reply(self, message: Message) -> None:
        assert self.bus is not None
        self.bus.send(Message.new_method_return(message))

    def _error(self, message: Message, name: str, text: str) -> None:
        assert self.bus is not None
        self.bus.send(Message.new_error(message, name, text))

    async def call(
        self,
        path: str,
        interface: str,
        member: str,
        signature: str,
        body: list[Any],
        destination: str | None = None,
    ) -> Message:
        """Call the robot, or ``destination``; the reply. ``ConnectionError`` for an error."""
        assert self.bus is not None
        reply = await self.bus.call(
            Message(
                destination=destination or self.robot,
                path=path,
                interface=interface,
                member=member,
                signature=signature,
                body=body,
            )
        )
        assert reply is not None
        if reply.message_type is MessageType.ERROR:
            raise ConnectionError(f"{member}: {reply.error_name}: {reply.body}")
        return reply
