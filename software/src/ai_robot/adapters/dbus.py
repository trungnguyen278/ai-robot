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
