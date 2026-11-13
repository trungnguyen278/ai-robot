# SPDX-License-Identifier: MIT
"""The link that apps set the robot up through: Bluetooth LE on the robot
(platform/protocols/setup/README.md; docs/decisions/0015-set-up.md)."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass

from ai_robot.ports.base import Port


@dataclass(frozen=True, slots=True)
class Connected:
    """An app connected. ``peer`` names it, as the adapter knows it: a peer connects
    again only after its ``Disconnected``."""

    peer: str


@dataclass(frozen=True, slots=True)
class Received:
    """A whole message from ``peer``, as it was sent."""

    peer: str
    message: bytes


@dataclass(frozen=True, slots=True)
class Disconnected:
    """``peer`` is gone: it left, or the robot dropped it."""

    peer: str


LinkEvent = Connected | Received | Disconnected


class SetupLink(Port):
    """Offers the set-up service while set-up is open, and carries whole messages to
    and from the apps that connect. Splitting messages into the transport's fragments,
    and joining them, is the adapter's work."""

    @abstractmethod
    async def start(self, name: str) -> None:
        """Offer the set-up service, and advertise it under ``name``."""

    @abstractmethod
    async def advertise(self, on: bool) -> None:
        """Advertise the service while it is offered, or stop advertising it. Apps
        already connected stay connected."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop offering the service: stop advertising, and disconnect every app."""

    @abstractmethod
    async def event(self) -> LinkEvent:
        """Wait for the next connection, message or disconnection."""

    @abstractmethod
    async def send(self, peer: str, message: bytes) -> None:
        """Send ``message`` to ``peer``, whole. Nothing happens if ``peer`` is gone."""

    @abstractmethod
    async def drop(self, peer: str) -> None:
        """Disconnect ``peer``. A ``Disconnected`` event follows, if it was connected."""
