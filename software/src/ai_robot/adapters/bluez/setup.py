# SPDX-License-Identifier: MIT
"""The set-up link over Bluetooth LE, through BlueZ (platform/protocols/setup/README.md, The
Bluetooth service): a GATT service and an advertisement, on D-Bus.

BlueZ's GATT server calls this application over D-Bus: ``WriteValue`` for each fragment an
app writes, and ``AcquireNotify`` when an app enables indications. That call takes one end
of a socket pair for that app alone: BlueZ indicates each packet the robot writes to the
other end, to that app only, and writes one byte back for each confirmation. It closes its
end when the app disables indications or disconnects (BlueZ 5.85,
doc/org.bluez.GattCharacteristic.rst and src/gatt-database.c).

BlueZ forgets what the robot registered when it stops, as installing a ``bluez`` package
does (its ``postinst``), or when the controller goes away. The link follows BlueZ's owner
on the bus and the managers on the controller's object, as BlueZ's own client does
(gdbus/client.c) and PipeWire (spa/plugins/bluez5/bluez5-dbus.c, 1.4.9): what BlueZ loses
ends its apps, and the link registers it again once BlueZ offers its managers again.
"""

# No `from __future__ import annotations` here: dbus-fast reads the annotations of the
# methods and properties below at run time, as D-Bus signatures.

import asyncio
import contextlib
import functools
import logging
import socket
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any

from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.annotations import DBusBool, DBusBytes, DBusDict, DBusSignature, DBusStr
from dbus_fast.constants import PropertyAccess
from dbus_fast.service import ServiceInterface, dbus_method, dbus_property

from ai_robot.adapters.bluez.fragments import FragmentError, Joiner, split
from ai_robot.adapters.dbus import OBJECT_MANAGER, Owner, call, connect, watch
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
    write; it leaves when BlueZ says the device disconnected, when BlueZ or the controller
    goes away, or when it is dropped. The link acts only on signals that BlueZ sent.
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
        self._bluez = Owner(BLUEZ)
        self._managers: set[str] = set()  # the interfaces BlueZ offers on the controller
        self._heard = 0  # changes of BlueZ or of them, so that an older listing undoes none
        # What the robot offers: the service, from start to stop, and its advertisement.
        self._offered = False
        self._advertising = False
        # What BlueZ holds of it, and how often BlueZ lost some, so that a registration
        # that ends after BlueZ lost it is not taken as held.
        self._registered = False
        self._advertised = False
        self._losses = 0
        self._registering = asyncio.Lock()
        self._apps: dict[str, _App] = {}  # device path -> the app connected through it
        self._channels: dict[str, _Channel] = {}  # device path -> its channel for indications
        self._changed = asyncio.Event()  # set, then replaced, when a channel comes or an app leaves
        self._events: asyncio.Queue[LinkEvent] = asyncio.Queue()
        self._tasks: set[asyncio.Task[None]] = set()  # started from what BlueZ says

    @property
    def acquired(self) -> bool:
        """Whether an app holds a channel for indications."""
        return bool(self._channels)

    async def open(self) -> None:
        """Connect to the bus, and learn whether BlueZ runs and what it offers. BlueZ
        need not run yet."""
        self._bus = await connect(self.bus_address, unix_fds=True)
        self._bus.add_message_handler(self._on_message)
        await watch(
            self._bus,
            f"type='signal',sender='{BLUEZ}',interface='{PROPERTIES}',"
            f"member='PropertiesChanged',path_namespace='{self.adapter_path}'",
        )
        # BlueZ's object manager is its root's (attach_object_manager, gdbus/object.c).
        await watch(
            self._bus, f"type='signal',sender='{BLUEZ}',interface='{OBJECT_MANAGER}',path='/'"
        )
        self._bus.export(SERVICE_PATH, self._service)
        self._bus.export(REQUEST_PATH, self._request)
        self._bus.export(ANSWER_PATH, self._answer)
        self._bus.export(ADVERTISEMENT_PATH, self._advertisement)
        await self._bluez.follow(self._bus)
        if self._bluez.unique is not None:
            with contextlib.suppress(ConnectionError):  # it is leaving: it says so
                await self._list()

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.stop()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for channel in list(self._channels.values()):
            channel.close()
        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None

    # ---- the port ----

    async def start(self, name: str) -> None:
        """Offer the service, then advertise it. ``ConnectionError`` if BlueZ does not run,
        does not offer both on the controller, or refuses: then nothing is offered."""
        self._advertisement.local = short_name(name)
        if self._offered:
            await self.advertise(True)
            return
        if not {GATT_MANAGER, ADVERTISING_MANAGER} <= self._managers:
            raise ConnectionError("BlueZ offers no GATT service and advertising on the controller")
        self._offered = self._advertising = True
        try:
            await self._sync()
        except BaseException:
            self._offered = self._advertising = False
            with contextlib.suppress(ConnectionError):
                await self._sync()
            raise

    async def advertise(self, on: bool) -> None:
        """Advertise while the service is offered, or stop. While BlueZ is away, the link
        keeps what it was asked, and advertises once BlueZ is back."""
        self._advertising = on
        await self._sync()

    async def stop(self) -> None:
        await self.advertise(False)
        for peer in list(self._apps):
            await self.drop(peer)
        for channel in list(self._channels.values()):  # apps that never wrote
            channel.close()
        self._offered = False
        await self._sync()

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
            await self._bluez_call(peer, DEVICE, "Disconnect")

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
            self._spawn(self.drop(peer))
            return
        if message is not None:
            self._events.put_nowait(Received(peer, message))

    def _on_message(self, message: Message) -> bool | None:
        """BlueZ asks for a channel; BlueZ comes or goes; or it says what changed: the
        managers it offers on the controller, or a device's connection. Only signals that
        BlueZ sent count, as the bus says who sent each."""
        if message.message_type is MessageType.METHOD_CALL:
            if (message.path, message.interface, message.member) == (
                ANSWER_PATH,
                CHARACTERISTIC,
                "AcquireNotify",
            ):
                self._acquire(message)
                return True
            return None
        if self._bluez.changed(message):
            self._heard += 1
            if self._offered and self._bluez.unique is None:
                log.warning("BlueZ stopped: set-up's apps are gone, and its service waits")
            self._lose(self._managers)
            if self._bluez.unique is not None:
                self._spawn(self._restore())
            return None
        if message.message_type is not MessageType.SIGNAL or not self._bluez.sent(message):
            return None
        if message.member == "PropertiesChanged" and message.body and message.body[0] == DEVICE:
            connected = message.body[1].get("Connected")
            if connected is not None and not connected.value:
                self._left(message.path)
        elif (
            message.interface == OBJECT_MANAGER
            and message.body
            and message.body[0] == self.adapter_path
        ):
            self._heard += 1
            if message.member == "InterfacesAdded":
                self._managers |= set(message.body[1])
                self._spawn(self._restore(listed=True))
            elif message.member == "InterfacesRemoved":
                self._lose(set(message.body[1]))
        return None

    def _lose(self, managers: set[str]) -> None:
        """BlueZ no longer offers ``managers`` on the controller, and lost what the robot
        registered with them. Apps leave with the GATT manager: the controller is gone."""
        managers = set(managers)  # it may be the link's own set
        self._managers -= managers
        if not managers & {GATT_MANAGER, ADVERTISING_MANAGER}:
            return
        self._losses += 1
        if ADVERTISING_MANAGER in managers:
            self._advertised = False
        if GATT_MANAGER in managers:
            self._registered = False
            for peer in list(self._apps):
                self._left(peer)
            for channel in list(self._channels.values()):
                channel.close()

    async def _list(self) -> None:
        """Learn which managers BlueZ offers on the controller."""
        while True:
            heard = self._heard
            (objects,) = await self._bluez_call("/", OBJECT_MANAGER, "GetManagedObjects")
            if self._heard == heard:  # else a change came meanwhile: list again
                self._managers = set(objects.get(self.adapter_path, {}))
                return

    async def _restore(self, *, listed: bool = False) -> None:
        """Register again what BlueZ lost, once it offers its managers again."""
        registered = self._registered
        try:
            if not listed:
                await self._list()
            await self._sync()
        except ConnectionError as error:
            log.warning("set-up cannot register its service with BlueZ again: %s", error)
            return
        if self._registered and not registered:
            log.info("set-up's service is registered with BlueZ again")

    async def _sync(self) -> None:
        """Have BlueZ hold what the robot offers, as far as BlueZ offers its managers:
        the service, then its advertisement."""
        async with self._registering:
            if self._advertised and not self._advertising:
                self._advertised = False
                await self._bluez_call(
                    self.adapter_path,
                    ADVERTISING_MANAGER,
                    "UnregisterAdvertisement",
                    "o",
                    [ADVERTISEMENT_PATH],
                )
            if self._registered and not self._offered:
                self._registered = False
                await self._bluez_call(
                    self.adapter_path, GATT_MANAGER, "UnregisterApplication", "o", [ROOT]
                )
            losses = self._losses
            if self._offered and not self._registered and GATT_MANAGER in self._managers:
                await self._bluez_call(
                    self.adapter_path, GATT_MANAGER, "RegisterApplication", "oa{sv}", [ROOT, {}]
                )
                self._registered = self._losses == losses
            if (
                self._advertising
                and self._registered
                and not self._advertised
                and ADVERTISING_MANAGER in self._managers
            ):
                await self._bluez_call(
                    self.adapter_path,
                    ADVERTISING_MANAGER,
                    "RegisterAdvertisement",
                    "oa{sv}",
                    [ADVERTISEMENT_PATH, {}],
                )
                self._advertised = self._losses == losses

    def _spawn(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.get_running_loop().create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

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

    async def _bluez_call(
        self, path: str, interface: str, member: str, signature: str = "", body: Sequence[Any] = ()
    ) -> list[Any]:
        assert self._bus is not None, "the link is not open"
        return await call(self._bus, BLUEZ, path, interface, member, signature, body)


def _mtu(options: dict[str, Variant]) -> int:
    """The ATT MTU BlueZ gives in ``options``."""
    return int(options["mtu"].value) if "mtu" in options else DEFAULT_MTU
