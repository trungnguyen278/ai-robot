# SPDX-License-Identifier: MIT
"""A simulated factory reset, kept in memory: it erases nothing, and the robot starts
again in this process."""

from __future__ import annotations

from ai_robot.ports.factory_reset import FactoryReset, Restart


class SimFactoryReset(FactoryReset):
    """Counts its erases. ``pending`` starts as a reset cut short, if a test says so."""

    def __init__(self, *, pending: bool) -> None:
        self.resetting = pending
        self.erased = 0

    async def pending(self) -> bool:
        return self.resetting

    async def begin(self) -> None:
        self.resetting = True

    async def erase(self) -> Restart:
        self.erased += 1
        self.resetting = False
        return Restart.PROCESS
