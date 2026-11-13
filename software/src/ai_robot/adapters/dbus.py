# SPDX-License-Identifier: MIT
"""D-Bus through dbus-fast: what the adapters of system services share (``bluez/``,
``networkmanager/``, ``timedated/``)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from dbus_fast import BusType, Message, MessageType
from dbus_fast.aio import MessageBus

BUS_NAME = "org.freedesktop.DBus"  # the bus itself (the D-Bus specification)
BUS_PATH = "/org/freedesktop/DBus"
OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"


async def connect(address: str, *, unix_fds: bool = False) -> MessageBus:
    """A connection to the bus at ``address``: ``system``, or another bus's address, as
    the tests use. ``unix_fds`` lets it pass file descriptors."""
    if address == "system":
        return await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=unix_fds).connect()
    return await MessageBus(bus_address=address, negotiate_unix_fd=unix_fds).connect()


async def call(
    bus: MessageBus,
    destination: str,
    path: str,
    interface: str,
    member: str,
    signature: str = "",
    body: Sequence[Any] = (),
) -> list[Any]:
    """Call ``interface.member`` on ``path`` of ``destination``; its reply's values.
    ``ConnectionError`` for an error, or no reply."""
    reply = await bus.call(
        Message(
            destination=destination,
            path=path,
            interface=interface,
            member=member,
            signature=signature,
            body=list(body),
        )
    )
    if reply is None or reply.message_type is MessageType.ERROR:
        detail = reply.body[0] if reply is not None and reply.body else "no reply"
        name = reply.error_name if reply is not None else "?"
        raise ConnectionError(f"{interface}.{member} failed: {name}: {detail}")
    return list(reply.body)


async def watch(bus: MessageBus, rule: str) -> None:
    """Ask the bus for the signals that match ``rule``."""
    await call(bus, BUS_NAME, BUS_PATH, BUS_NAME, "AddMatch", "s", [rule])


class Owner:
    """Who owns the well-known ``name``, a system service's, on a bus: the unique name
    of that service's connection, or None, kept current as the bus announces each change
    (``NameOwnerChanged``, the D-Bus specification).

    A message comes from the service only if its sender is that unique name: the bus sets
    every message's sender. A match rule's ``sender`` is no such check: the bus delivers a
    signal sent to one connection whatever rules that connection asked for, and the system
    bus lets any connection send signals. BlueZ's own D-Bus client filters signals so
    (gdbus/watch.c, BlueZ 5.85).
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.unique: str | None = None
        self._changes = 0  # changes heard, so that an older answer does not undo one

    async def follow(self, bus: MessageBus) -> None:
        """Start following the owner. ``changed`` must see each message ``bus`` receives
        from now on."""
        await watch(
            bus,
            f"type='signal',sender='{BUS_NAME}',interface='{BUS_NAME}',"
            f"member='NameOwnerChanged',arg0='{self.name}'",
        )
        heard = self._changes
        try:
            (owner,) = await call(
                bus, BUS_NAME, BUS_PATH, BUS_NAME, "GetNameOwner", "s", [self.name]
            )
        except ConnectionError:  # no one owns it
            owner = None
        if self._changes == heard:
            self.unique = owner

    def changed(self, message: Message) -> bool:
        """Whether ``message`` is the bus saying that the name changed owner; if so,
        ``unique`` is the new one's."""
        if not (
            message.sender == BUS_NAME  # only the bus sends as the bus
            and message.member == "NameOwnerChanged"
            and message.body[0] == self.name
        ):
            return False
        self._changes += 1
        self.unique = message.body[2] or None
        return True

    def sent(self, message: Message) -> bool:
        """Whether the service sent ``message``."""
        return self.unique is not None and message.sender == self.unique
