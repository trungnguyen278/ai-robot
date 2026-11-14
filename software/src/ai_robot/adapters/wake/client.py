# SPDX-License-Identifier: MIT
"""The robot's side of ``ai-robot-wake`` (``helper.py``)."""

from __future__ import annotations

import asyncio

from ai_robot.adapters.wake.helper import ANSWER_OK, REQUEST
from ai_robot.core.checks import positive
from ai_robot.ports.display_power import DisplayPower


class HelperDisplayPower(DisplayPower):
    """Asks the helper listening on the Unix socket ``path`` to wake the screen, and
    waits at most ``timeout`` seconds for its answer."""

    def __init__(self, *, path: str, timeout: float) -> None:
        if not path:
            raise ValueError("the helper's socket needs a path")
        self.path = path
        self.timeout = positive("timeout", timeout)

    async def wake(self) -> None:
        try:
            async with asyncio.timeout(self.timeout):
                reader, writer = await asyncio.open_unix_connection(self.path)
                try:
                    writer.write(REQUEST)
                    await writer.drain()
                    answer = await reader.readline()
                finally:
                    writer.close()
                    await writer.wait_closed()
        except TimeoutError:
            raise OSError("the wake helper did not answer in time") from None
        if answer != ANSWER_OK:
            raise OSError("the wake helper refused the request")
