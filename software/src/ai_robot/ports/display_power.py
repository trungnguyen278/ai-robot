# SPDX-License-Identifier: MIT
"""The screen's power: turning on a screen that the system turned off for idleness."""

from __future__ import annotations

from abc import abstractmethod

from ai_robot.ports.base import Port


class DisplayPower(Port):
    """The power of the robot's screen (docs/decisions/0021-sleep-and-wake.md).

    The page that shows the face keeps the screen on while the robot is awake, and lets
    the system turn it off once the robot sleeps. A dark screen does not light up for a
    page, so the robot wakes it through this port.
    """

    @abstractmethod
    async def wake(self) -> None:
        """Turn the screen on if it is off; nothing if it is on. ``OSError`` if the
        system cannot be reached."""
