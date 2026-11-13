# SPDX-License-Identifier: MIT
"""Following a service's owner on the bus (``adapters/dbus.py``), from the bus's own
messages: the bus sets each message's sender (the D-Bus specification)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("dbus_fast")

from dbus_fast import Message, MessageType

from ai_robot.adapters.dbus import BUS_NAME, BUS_PATH, Owner

NAME = "org.bluez"


def owner_changed(name: str, old: str, new: str, sender: str = BUS_NAME) -> Message:
    return Message(
        message_type=MessageType.SIGNAL,
        sender=sender,
        path=BUS_PATH,
        interface=BUS_NAME,
        member="NameOwnerChanged",
        signature="sss",
        body=[name, old, new],
    )


def test_the_owner_follows_what_the_bus_says() -> None:
    owner = Owner(NAME)
    assert not owner.sent(Message(path="/", member="x"))  # no owner: nothing is its
    assert owner.changed(owner_changed(NAME, "", ":1.7"))
    assert owner.unique == ":1.7"
    assert owner.sent(Message(sender=":1.7", path="/", member="x"))
    assert not owner.sent(Message(sender=":1.8", path="/", member="x"))
    assert owner.changed(owner_changed(NAME, ":1.7", ""))
    assert owner.unique is None
    assert not owner.sent(Message(sender=":1.7", path="/", member="x"))


@pytest.mark.parametrize(
    "message",
    [
        owner_changed(NAME, ":1.7", "", sender=":1.9"),  # another program says so
        owner_changed("org.example", ":1.7", ""),  # another name
        Message(
            message_type=MessageType.SIGNAL,
            sender=BUS_NAME,
            path=BUS_PATH,
            interface=BUS_NAME,
            member="NameAcquired",
            signature="s",
            body=[NAME],
        ),
    ],
)
def test_only_the_bus_moves_the_owner_of_the_name(message: Message) -> None:
    owner = Owner(NAME)
    owner.unique = ":1.7"
    assert not owner.changed(message)
    assert owner.unique == ":1.7"


class Bus:
    """A bus that answers ``GetNameOwner`` with ``owner``, after the name has changed
    owner to ``new``, as a reply can come after a change it does not know of."""

    def __init__(self, owner: Owner, answer: str, new: str | None) -> None:
        self.owner, self.answer, self.new = owner, answer, new

    async def call(self, message: Message) -> Message:
        reply = {"message_type": MessageType.METHOD_RETURN, "reply_serial": 1}
        if message.member == "AddMatch":
            return Message(**reply)
        if self.new is not None:
            self.owner.changed(owner_changed(NAME, self.answer, self.new))
        if not self.answer:
            no_owner = "org.freedesktop.DBus.Error.NameHasNoOwner"
            return Message(**reply | {"message_type": MessageType.ERROR, "error_name": no_owner})
        return Message(**reply, signature="s", body=[self.answer])


@pytest.mark.parametrize(
    ("answer", "new", "unique"),
    [(":1.7", None, ":1.7"), ("", None, None), (":1.7", ":1.8", ":1.8"), (":1.7", "", None)],
)
def test_following_starts_from_the_bus_answer_unless_a_change_came(
    answer: str, new: str | None, unique: str | None
) -> None:
    owner = Owner(NAME)
    bus: Any = Bus(owner, answer, new)
    asyncio.run(owner.follow(bus))
    assert owner.unique == unique
