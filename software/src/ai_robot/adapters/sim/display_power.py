# SPDX-License-Identifier: MIT
"""A simulated screen power, for computers whose screen the robot leaves alone."""

from __future__ import annotations

from ai_robot.ports.display_power import DisplayPower


class SimDisplayPower(DisplayPower):
    """Counts the wakes, so that tests can see them."""

    def __init__(self) -> None:
        self.wakes = 0

    async def wake(self) -> None:
        self.wakes += 1
