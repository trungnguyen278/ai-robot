# SPDX-License-Identifier: MIT
"""The set-up link over Bluetooth LE, through BlueZ (platform/protocols/setup/README.md, The
Bluetooth service): a GATT service and an advertisement, on D-Bus.

BlueZ's GATT server calls this application over D-Bus: ``WriteValue`` for each fragment an
app writes, and ``AcquireNotify`` when an app enables indications. That call takes one end
of a socket pair for that app alone: BlueZ indicates each packet the robot writes to the
other end, to that app only, and writes one byte back for each confirmation. It closes its
end when the app disables indications or disconnects (BlueZ 5.85,
doc/org.bluez.GattCharacteristic.rst and src/gatt-database.c).
"""

# No `from __future__ import annotations` here: dbus-fast reads the annotations of the
# methods and properties below at run time, as D-Bus signatures.

import asyncio
import contextlib
import functools
import logging
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any

from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.annotations import DBusBool, DBusBytes, DBusDict, DBusSignature, DBusStr
from dbus_fast.constants import PropertyAccess
from dbus_fast.service import ServiceInterface, dbus_method, dbus_property

from ai_robot.adapters.bluez.fragments import FragmentError, Joiner, split
from ai_robot.adapters.dbus import call, connect, watch
from ai_robot.core.checks import positive
from ai_robot.ports.setup import Connected, Disconnected, LinkEvent, Received, SetupLink

log = logging.getLogger(__name__)

# Fixed by the protocol (platform/protocols/setup/README.md, The Bluetooth service).
SERVICE_UUID = "eaf35dcb-e774-4881-b0c6-4990127a1289"
REQUEST_UUID = "eaf35dcc-e774-4881-b0c6-4990127a1289"
ANSWER_UUID = "eaf35dcd-e774-4881-b0c6-4990127a1289"

# BlueZ's D-Bus interfaces (doc/org.bluez.*.rst in BlueZ's sources).
BLUEZ = "org.bluez"
GATT_MANAGER = "org.bluez.GattManager1"
ADVERTISING_MANAGER = "org.bluez.LEAdvertisingManager1"
CHARACTERISTIC = "org.bluez.GattCharacteristic1"
DEVICE = "org.bluez.Device1"
PROPERTIES = "org.freedesktop.DBus.Properties"
# The error BlueZ takes as final when it asks for a channel: it then does not fall back on
# StartNotify (acquire_notify_reply in src/gatt-database.c, BlueZ 5.85).
FAILED = "org.bluez.Error.Failed"
DEFAULT_MTU = 23  # the ATT MTU until a larger one is exchanged (Bluetooth Core, Vol 3, Part F)
# BlueZ writes one byte, 0x01, for each confirmation (sock_io_write in src/gatt-database.c).
CONFIRMATION_BYTES = 1
# A legacy advertisement holds 31 bytes: the flags take 3 and the service's 128-bit UUID
# 18, which leaves 8 for the name, after its 2 bytes of length and type.
NAME_BYTES = 8

# The objects on the bus. BlueZ wants the GATT application's objects, and only them,
# under its root (doc/org.bluez.GattManager.rst): the advertisement lives beside it.
ROOT = "/ai_robot/setup"
SERVICE_PATH = f"{ROOT}/service0"
REQUEST_PATH = f"{SERVICE_PATH}/request"
ANSWER_PATH = f"{SERVICE_PATH}/answer"
ADVERTISEMENT_PATH = "/ai_robot/advertisement0"

DBusStrings = Annotated[list[str], DBusSignature("as")]
DBusPath = Annotated[str, DBusSignature("o")]


def short_name(name: str) -> str:
    """``name``, cut to what fits in the advertisement, on a whole character."""
    data = name.encode("utf-8")[:NAME_BYTES]
    return data.decode("utf-8", errors="ignore")


class _Service(ServiceInterface):
    def __init__(self) -> None:
        super().__init__("org.bluez.GattService1")

    @dbus_property(access=PropertyAccess.READ, name="UUID")
    def uuid(self) -> DBusStr:
        return SERVICE_UUID

    @dbus_property(access=PropertyAccess.READ, name="Primary")
    def primary(self) -> DBusBool:
        return True


class _Request(ServiceInterface):
    """The characteristic apps write their fragments to."""

    def __init__(self, link: "BluezSetupLink") -> None:
        super().__init__(CHARACTERISTIC)
        self._link = link

    @dbus_property(access=PropertyAccess.READ, name="UUID")
    def uuid(self) -> DBusStr:
        return REQUEST_UUID

    @dbus_property(access=PropertyAccess.READ, name="Service")
    def service(self) -> DBusPath:
        return SERVICE_PATH

    @dbus_property(access=PropertyAccess.READ, name="Flags")
    def flags(self) -> DBusStrings:
        return ["write"]

    @dbus_method(name="WriteValue")
    def write_value(self, value: DBusBytes, options: DBusDict) -> None:
        self._link.written(bytes(value), options)


class _Answer(ServiceInterface):
    """The characteristic the robot indicates its fragments on, through each app's channel.
    The link answers ``AcquireNotify`` itself (``BluezSetupLink._acquire``), since it
    hands over a file descriptor."""

    def __init__(self, link: "BluezSetupLink") -> None:
        super().__init__(CHARACTERISTIC)
        self._link = link

    @dbus_property(access=PropertyAccess.READ, name="UUID")
    def uuid(self) -> DBusStr:
        return ANSWER_UUID

    @dbus_property(access=PropertyAccess.READ, name="Service")
    def service(self) -> DBusPath:
        return SERVICE_PATH

    @dbus_property(access=PropertyAccess.READ, name="Flags")
    def flags(self) -> DBusStrings:
        return ["indicate"]

    @dbus_property(access=PropertyAccess.READ, name="NotifyAcquired")
    def notify_acquired(self) -> DBusBool:
        # Its presence has BlueZ ask for a channel; its value, whether an app holds one.
        return self._link.acquired


class _Advertisement(ServiceInterface):
    def __init__(self) -> None:
        super().__init__("org.bluez.LEAdvertisement1")
        self.local = ""  # not `name`: ServiceInterface's name is the interface's

    @dbus_property(access=PropertyAccess.READ, name="Type")
    def kind(self) -> DBusStr:
        return "peripheral"

    @dbus_property(access=PropertyAccess.READ, name="ServiceUUIDs")
    def service_uuids(self) -> DBusStrings:
        return [SERVICE_UUID]

    @dbus_property(access=PropertyAccess.READ, name="LocalName")
    def local_name(self) -> DBusStr:
        return self.local

    @dbus_property(access=PropertyAccess.READ, name="Discoverable")
    def discoverable(self) -> DBusBool:
        return True

    @dbus_method(name="Release")
    def release(self) -> None:
        log.info("BlueZ released the set-up advertisement")


class _Channel:
    """One app's channel for indications: the robot's end of the socket pair it gave BlueZ
    through ``AcquireNotify``. ``closed`` is called once it closes, from either end."""

    def __init__(self, end: socket.socket, closed: Callable[["_Channel"], None]) -> None:
        self._end = end
        self._closed = closed
        self._heard = asyncio.Event()  # a confirmation came, or the channel closed
        self.open = True
        self._loop = asyncio.get_running_loop()
        self._loop.add_reader(end.fileno(), self._readable)

    async def indicate(self, fragment: bytes, timeout: float) -> bool:
        """Whether the app confirmed ``fragment`` within ``timeout`` seconds."""
        self._heard.clear()
        try:
            await self._loop.sock_sendall(self._end, fragment)
        except OSError:  # BlueZ shut its end down
            return False
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._heard.wait(), timeout)
        return self.open and self._heard.is_set()

    def close(self) -> None:
        if not self.open:
            return
        self.open = False
        self._loop.remove_reader(self._end.fileno())
        self._end.close()
        self._heard.set()
        self._closed(self)

    def _readable(self) -> None:
        try:
            if self._end.recv(CONFIRMATION_BYTES):
                self._heard.set()
                return
        except BlockingIOError:
            return
        except OSError:
            pass
        self.close()  # BlueZ closed its end


@dataclass
class _App:
    """What the link keeps of an app connected."""

    mtu: int = DEFAULT_MTU
    joiner: Joiner = field(default_factory=Joiner)
    # One message at a time: each fragment is confirmed before the next.
    sending: asyncio.Lock = field(default_factory=asyncio.Lock)


class BluezSetupLink(SetupLink):
    """Offers the set-up service on the Bluetooth controller ``adapter`` (``hci0``),
    through BlueZ on the D-Bus ``bus``: ``system``, or the address of another bus, as the
    tests use. An app that has not enabled indications, or does not confirm one, within
    ``confirm_timeout`` seconds of the robot's sending is dropped.

    Apps are named by their BlueZ device's object path. One connects with its first
    write; it leaves when BlueZ says the device disconnected, or when it is dropped.
    """

    def __init__(self, *, adapter: str, bus: str, confirm_timeout: float) -> None:
        if not adapter:
            raise ValueError("adapter must not be empty")
        self.adapter_path = f"/org/bluez/{adapter}"
        self.bus_address = bus
        self.confirm_timeout = positive("confirm_timeout", confirm_timeout)
        self._bus: MessageBus | None = None
        self._service = _Service()
        self._request = _Request(self)
        self._answer = _Answer(self)
        self._advertisement = _Advertisement()
        self._offered = False
        self._advertised = False
        self._apps: dict[str, _App] = {}  # device path -> the app connected through it
        self._channels: dict[str, _Channel] = {}  # device path -> its channel for indications
        self._changed = asyncio.Event()  # set, then replaced, when a channel comes or an app leaves
        self._events: asyncio.Queue[LinkEvent] = asyncio.Queue()
        self._tasks: set[asyncio.Task[None]] = set()  # drops started from BlueZ's calls

    @property
    def acquired(self) -> bool:
        """Whether an app holds a channel for indications."""
        return bool(self._channels)

    async def open(self) -> None:
        self._bus = await connect(self.bus_address, unix_fds=True)
        self._bus.add_message_handler(self._on_message)
        await watch(
            self._bus,
            f"type='signal',sender='{BLUEZ}',interface='{PROPERTIES}',"
            f"member='PropertiesChanged',path_namespace='{self.adapter_path}'",
        )
        self._bus.export(SERVICE_PATH, self._service)
        self._bus.export(REQUEST_PATH, self._request)
        self._bus.export(ANSWER_PATH, self._answer)
        self._bus.export(ADVERTISEMENT_PATH, self._advertisement)

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.stop()
        for channel in list(self._channels.values()):
            channel.close()
        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None

    # ---- the port ----

    async def start(self, name: str) -> None:
        self._advertisement.local = short_name(name)
        if not self._offered:
            await self._bluez(
                self.adapter_path, GATT_MANAGER, "RegisterApplication", "oa{sv}", [ROOT, {}]
            )
            self._offered = True
        await self.advertise(True)

    async def advertise(self, on: bool) -> None:
        if on and self._offered and not self._advertised:
            await self._bluez(
                self.adapter_path,
                ADVERTISING_MANAGER,
                "RegisterAdvertisement",
                "oa{sv}",
                [ADVERTISEMENT_PATH, {}],
            )
            self._advertised = True
        elif not on and self._advertised:
            self._advertised = False
            await self._bluez(
                self.adapter_path,
                ADVERTISING_MANAGER,
                "UnregisterAdvertisement",
                "o",
                [ADVERTISEMENT_PATH],
            )

    async def stop(self) -> None:
        await self.advertise(False)
        for peer in list(self._apps):
            await self.drop(peer)
        for channel in list(self._channels.values()):  # apps that never wrote
            channel.close()
        if self._offered:
            self._offered = False
            await self._bluez(self.adapter_path, GATT_MANAGER, "UnregisterApplication", "o", [ROOT])

    async def event(self) -> LinkEvent:
        return await self._events.get()

    async def send(self, peer: str, message: bytes) -> None:
        app = self._apps.get(peer)
        if app is None:
            return
        async with app.sending:
            for fragment in split(message, app.mtu):
                channel = await self._channel(peer, app)
                if self._apps.get(peer) is not app:
                    return  # it left
                if channel is None:
                    log.warning("an app did not enable set-up's indications: dropped")
                    await self.drop(peer)
                    return
                if not await channel.indicate(fragment, self.confirm_timeout):
                    log.warning("an app did not confirm a set-up fragment: dropped")
                    await self.drop(peer)
                    return

    async def drop(self, peer: str) -> None:
        if peer not in self._apps:
            return
        self._left(peer)
        with contextlib.suppress(ConnectionError):  # it may be gone already
            await self._bluez(peer, DEVICE, "Disconnect")

    # ---- what BlueZ tells the link ----

    def written(self, fragment: bytes, options: dict[str, Variant]) -> None:
        """A fragment an app wrote to the request characteristic."""
        peer = self._device(options)
        if peer is None:
            log.warning("a set-up fragment came from no known device; ignored")
            return
        app = self._apps.get(peer)
        if app is None:
            app = self._apps[peer] = _App()
            self._events.put_nowait(Connected(peer))
        app.mtu = _mtu(options)
        try:
            message = app.joiner.add(fragment)
        except FragmentError as error:
            log.warning("an app sent a fragment that does not fit (%s): dropped", error)
            task = asyncio.get_running_loop().create_task(self.drop(peer))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return
        if message is not None:
            self._events.put_nowait(Received(peer, message))

    def _on_message(self, message: Message) -> bool | None:
        """BlueZ asks for a channel, or a device's properties changed: one that
        disconnects leaves."""
        if message.message_type is MessageType.METHOD_CALL:
            if (message.path, message.interface, message.member) == (
                ANSWER_PATH,
                CHARACTERISTIC,
                "AcquireNotify",
            ):
                self._acquire(message)
                return True
            return None
        if (
            message.message_type is MessageType.SIGNAL
            and message.member == "PropertiesChanged"
            and message.body
            and message.body[0] == DEVICE
        ):
            connected = message.body[1].get("Connected")
            if connected is not None and not connected.value:
                self._left(message.path)
        return None

    def _acquire(self, request: Message) -> None:
        """Answer ``AcquireNotify``: an app enabled indications. It gets a channel of its
        own, as bluetoothctl's GATT server makes one (client/gatt.c, ``create_sock``)."""
        assert self._bus is not None
        options = request.body[0] if request.signature == "a{sv}" else {}
        peer = self._device(options)
        if peer is None:
            self._bus.send(Message.new_error(request, FAILED, "no known device"))
            return
        try:
            ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        except OSError as error:
            log.warning("no channel for an app's indications (%s)", error.strerror)
            self._bus.send(Message.new_error(request, FAILED, "no channel"))
            return
        for end in (ours, theirs):
            end.setblocking(False)
        old = self._channels.get(peer)  # the app enabled indications again
        self._hold(peer, _Channel(ours, functools.partial(self._lost, peer)))
        if old is not None:
            old.close()
        # dbus-fast sends the descriptor when it writes the reply, and keeps no copy.
        sent = self._bus.send(
            Message.new_method_return(request, "hq", [0, _mtu(options)], unix_fds=[theirs.fileno()])
        )
        sent.add_done_callback(lambda _: theirs.close())

    def _lost(self, peer: str, channel: _Channel) -> None:
        """``channel`` closed."""
        if self._channels.get(peer) is channel:
            self._hold(peer, None)

    def _left(self, peer: str) -> None:
        """``peer`` is gone: by BlueZ's word, or dropped."""
        if (channel := self._channels.get(peer)) is not None:
            channel.close()
        if self._apps.pop(peer, None) is not None:
            self._events.put_nowait(Disconnected(peer))
            self._wake()

    def _hold(self, peer: str, channel: _Channel | None) -> None:
        """Keep ``channel`` as the channel of ``peer``, or none."""
        held = self.acquired
        if channel is None:
            del self._channels[peer]
        else:
            self._channels[peer] = channel
        if held != self.acquired:
            self._answer.emit_properties_changed({"NotifyAcquired": self.acquired})
        self._wake()

    def _wake(self) -> None:
        """Wake the sends waiting for a channel."""
        self._changed.set()
        self._changed = asyncio.Event()

    async def _channel(self, peer: str, app: _App) -> _Channel | None:
        """The channel of ``app``, once it has one, within ``confirm_timeout``; None if it
        has none by then, or has left."""
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(self.confirm_timeout):
                while self._apps.get(peer) is app:
                    if (channel := self._channels.get(peer)) is not None:
                        return channel
                    await self._changed.wait()
        return None

    def _device(self, options: dict[str, Variant]) -> str | None:
        """The device BlueZ names in ``options``, if it is one of this controller's."""
        peer = options["device"].value if "device" in options else ""
        return peer if peer.startswith(self.adapter_path + "/") else None

    async def _bluez(
        self, path: str, interface: str, member: str, signature: str = "", body: Sequence[Any] = ()
    ) -> list[Any]:
        assert self._bus is not None, "the link is not open"
        return await call(self._bus, BLUEZ, path, interface, member, signature, body)


def _mtu(options: dict[str, Variant]) -> int:
    """The ATT MTU BlueZ gives in ``options``."""
    return int(options["mtu"].value) if "mtu" in options else DEFAULT_MTU
