# SPDX-License-Identifier: MIT
"""Simulated screen."""

from __future__ import annotations

import asyncio
from typing import Any

from ai_robot.ports.screen import (
    Action,
    Close,
    Hold,
    Opened,
    Screen,
    ScreenEvent,
    ScreenState,
    Touch,
)


class SimScreen(Screen):
    """A screen without a renderer: it keeps what it was asked to show."""

    def __init__(self) -> None:
        self.state = ScreenState()
        self.updates = 0
        self.gaze: tuple[float, float] | None = None
        self.level = 0.0
        self._events: asyncio.Queue[ScreenEvent] = asyncio.Queue()

    async def show(self, state: ScreenState) -> None:
        self.state = state
        self.updates += 1

    async def look(self, target: tuple[float, float] | None) -> None:
        self.gaze = target

    async def voice(self, level: float) -> None:
        self.level = level

    async def event(self) -> ScreenEvent:
        return await self._events.get()

    def tap(self, x: float, y: float) -> None:
        """Simulate a tap at ``x``, ``y`` (fractions of the width and height)."""
        self._events.put_nowait(Touch(x, y))

    def hold(self, x: float, y: float) -> None:
        """Simulate a press held still at ``x``, ``y``."""
        self._events.put_nowait(Hold(x, y))

    def act(self, panel: str, item: str, value: Any = None) -> None:
        """Simulate the owner acting on ``item`` of the panel on screen."""
        self._events.put_nowait(Action(panel, item, value))

    def close_panel(self, panel: str) -> None:
        """Simulate the owner closing ``panel``."""
        self._events.put_nowait(Close(panel))

    def open_page(self) -> None:
        """Simulate a page that shows the face connecting."""
        self._events.put_nowait(Opened())
