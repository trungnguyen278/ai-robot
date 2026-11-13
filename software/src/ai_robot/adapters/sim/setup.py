# SPDX-License-Identifier: MIT
"""Simulated set-up link: no radio. Tests, and tools that play the app, connect to it and
exchange whole messages with the robot through the methods below the port's."""

from __future__ import annotations

import asyncio

from ai_robot.ports.setup import Connected, Disconnected, LinkEvent, Received, SetupLink


class SimSetupLink(SetupLink):
    """Apps connect with ``connect``, send with ``deliver``, read what the robot sent
    with ``receive``, and leave with ``disconnect``. As on a real link, what the robot
    sent before a connection ended can still be read; then ``receive`` says it ended."""

    def __init__(self) -> None:
        self.offered = False  # whether the set-up service is offered
        self.advertised: str | None = None  # the name it is advertised under, if it is
        self._name = ""
        self._events: asyncio.Queue[LinkEvent] = asyncio.Queue()
        self._peers: set[str] = set()  # the apps connected now
        # What the robot sent each app, then None once the connection ended.
        self._outboxes: dict[str, asyncio.Queue[bytes | None]] = {}

    # ---- the port ----

    async def start(self, name: str) -> None:
        self.offered = True
        self._name = name
        self.advertised = name

    async def advertise(self, on: bool) -> None:
        self.advertised = self._name if on and self.offered else None

    async def stop(self) -> None:
        self.offered = False
        self.advertised = None
        for peer in list(self._peers):
            await self.drop(peer)

    async def event(self) -> LinkEvent:
        return await self._events.get()

    async def send(self, peer: str, message: bytes) -> None:
        if peer in self._peers:
            self._outboxes[peer].put_nowait(message)

    async def drop(self, peer: str) -> None:
        self.disconnect(peer)

    # ---- the app's side ----

    def connect(self, peer: str) -> None:
        """``peer`` connects, as an app would while the service is offered."""
        if not self.offered:
            raise ConnectionError("the set-up service is not offered")
        if peer in self._peers:
            raise ConnectionError(f"{peer} is connected already")
        self._peers.add(peer)
        self._outboxes[peer] = asyncio.Queue()
        self._events.put_nowait(Connected(peer))

    def deliver(self, peer: str, message: bytes) -> None:
        """``peer`` sends ``message``."""
        if peer not in self._peers:
            raise ConnectionError(f"{peer} is not connected")
        self._events.put_nowait(Received(peer, message))

    def disconnect(self, peer: str) -> None:
        """``peer`` leaves."""
        if peer in self._peers:
            self._peers.discard(peer)
            self._outboxes[peer].put_nowait(None)
            self._events.put_nowait(Disconnected(peer))

    def connected(self, peer: str) -> bool:
        return peer in self._peers

    async def receive(self, peer: str) -> bytes:
        """The next message the robot sent to ``peer``; ``ConnectionError`` once the
        connection has ended and nothing is left to read."""
        outbox = self._outboxes.get(peer)
        if outbox is None:
            raise ConnectionError(f"{peer} never connected")
        message = await outbox.get()
        if message is None:
            outbox.put_nowait(None)  # the end stays, for the next read
            raise ConnectionError(f"{peer} is not connected")
        return message
