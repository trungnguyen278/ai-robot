# SPDX-License-Identifier: MIT
"""Base class of the robot's modules."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar, TypeVar

from ai_robot.core.bus import Bus
from ai_robot.ports import Port

P = TypeVar("P", bound=Port)


@dataclass(frozen=True)
class RobotInfo:
    """Facts about the robot that any module may need: the name shown to people, and,
    for the cloud and the app, its kind of device and its model (ai-platform ADR-0001)."""

    display_name: str
    kind: str
    model: str


@dataclass(frozen=True)
class ModuleContext:
    """What the application hands to a module."""

    name: str
    bus: Bus
    ports: Mapping[str, Port]
    robot: RobotInfo
    log: logging.Logger


class Module:
    """One function of the robot: voice, vision, screen, motion, ...

    - A module owns the ports named in ``owns``; no other module uses them.
    - It names in ``needs`` the modules it cannot work without, as one that waits for
      their answers on the bus: the robot does not start without them.
    - It talks to the other modules only through messages on the bus.
    - Its settings, the ``[modules.<name>]`` table, arrive as keyword arguments of
      ``__init__``, which raises ``ValueError`` for an invalid value.

    Once every adapter is open, the application calls ``start`` on each module in
    configuration order, then runs every module's ``run`` concurrently. When the robot
    stops, it cancels ``run`` and calls ``stop`` in reverse order, only for modules whose
    ``start`` succeeded. Subscribe to messages in ``__init__``, so that none published
    while the other modules start is missed.
    """

    owns: ClassVar[tuple[str, ...]] = ()
    needs: ClassVar[tuple[str, ...]] = ()  # modules that must be enabled with this one

    def __init__(self, ctx: ModuleContext) -> None:
        self.name = ctx.name
        self.bus = ctx.bus
        self.robot = ctx.robot
        self.log = ctx.log
        self._ports = ctx.ports

    def port(self, name: str, kind: type[P]) -> P:
        """The adapter of the owned port ``name``, checked to be a ``kind``."""
        if name not in self.owns:
            raise KeyError(f"module {self.name!r} does not own port {name!r}")
        adapter = self._ports[name]
        if not isinstance(adapter, kind):
            raise TypeError(f"port {name!r} is a {type(adapter).__name__}, not a {kind.__name__}")
        return adapter

    async def start(self) -> None:
        """Get ready: put the hardware in a known state, announce the initial state.

        Keep it short; long-running work belongs in ``run``. If the robot is stopped
        during startup, ``start`` is cancelled and ``stop`` is not called, so undo here
        what was already done.
        """

    async def run(self) -> None:
        """The module's main loop, cancelled when the robot stops. By default: wait."""
        await asyncio.Event().wait()

    async def stop(self) -> None:
        """Leave the hardware in a safe state. Called after ``run`` has been cancelled."""
