# SPDX-License-Identifier: MIT
"""The robot's factory reset: it erases what the robot keeps of its owner, its home and
itself, so that it starts again as a new robot (docs/decisions/0023-factory-reset.md)."""

from __future__ import annotations

from abc import abstractmethod
from enum import StrEnum

from ai_robot.ports.base import Port


class Restart(StrEnum):
    """Who starts the robot again once it is erased."""

    PROCESS = "process"  # the robot, in this process: the application starts again
    SYSTEM = "system"  # the system, which restarts to finish the erase: the robot waits


class FactoryReset(Port):
    """Erases the robot, and keeps that it is doing so until it is done.

    A reset keeps that it began before the robot leaves its home, and keeps it until the
    erase is done, so that a reset cut short, by a power cut for one, goes on at the next
    start instead of offering set-up with the old owner's data
    (platform/protocols/cloud/README.md, ``leave``).
    """

    @abstractmethod
    async def pending(self) -> bool:
        """Whether a reset began and was not done: the robot goes on with it."""

    @abstractmethod
    async def begin(self) -> None:
        """Keep that a reset began, on storage that outlives a power cut."""

    @abstractmethod
    async def erase(self) -> Restart:
        """Erase the robot, or have the system erase it, and say who starts it again.
        What says that a reset began goes last: with the rest, or once the rest is
        erased."""
