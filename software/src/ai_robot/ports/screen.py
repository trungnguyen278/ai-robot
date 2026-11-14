# SPDX-License-Identifier: MIT
"""Screen."""

from __future__ import annotations

import math
from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ai_robot.core.data import frozen
from ai_robot.ports.base import Port

# Fixed by the protocol (protocol/screen/vocabulary.json, panel_items).
PANEL_KINDS = ("toggle", "slider", "choice", "button", "info")


def scene_data(data: Mapping[str, Any]) -> Mapping[str, Any]:
    """A deep, read-only copy of a scene's ``data``, made of JSON values only
    (``core.data.frozen``); otherwise ``ValueError``. Converting dates and the like is
    the publisher's job, where their meaning is known; the screen protocol lists what
    each scene takes."""
    return frozen("scene data", data)


def _finite(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    return float(value)


@dataclass(frozen=True, slots=True)
class PanelItem:
    """One item of a panel (protocol/screen/README.md, Panels). ``value`` is a toggle's
    boolean, a slider's number, the value of the choice made, or an info's text; a
    button has none. ``choices`` are a choice's options, as (value, label) pairs."""

    id: str
    kind: str
    label: str
    value: Any = None
    help: str | None = None
    confirm: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    unit: str | None = None
    choices: tuple[tuple[Any, str], ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in PANEL_KINDS:
            raise ValueError(f"a panel item's kind is one of {PANEL_KINDS}, got {self.kind!r}")
        if not self.id or not self.label:
            raise ValueError("a panel item needs an id and a label")
        if self.confirm is not None and self.kind not in ("toggle", "button"):
            raise ValueError("only a toggle or a button asks for confirmation")
        if self.kind == "toggle" and not isinstance(self.value, bool):
            raise ValueError(f"toggle {self.id}: its value is a boolean")
        if self.kind == "slider":
            low = _finite(f"slider {self.id}: minimum", self.minimum)
            high = _finite(f"slider {self.id}: maximum", self.maximum)
            step = _finite(f"slider {self.id}: step", self.step)
            value = _finite(f"slider {self.id}: value", self.value)
            if not (low < high and step > 0 and low <= value <= high):
                raise ValueError(f"slider {self.id}: needs minimum <= value <= maximum, step > 0")
        if self.kind == "choice":
            object.__setattr__(self, "choices", tuple((v, str(t)) for v, t in self.choices))
            values = [value for value, _ in self.choices]
            if not values or self.value not in values:
                raise ValueError(f"choice {self.id}: its value must be one of its choices")
        if self.kind == "button" and self.value is not None:
            raise ValueError(f"button {self.id}: has no value")
        if self.kind == "info" and not isinstance(self.value, str):
            raise ValueError(f"info {self.id}: its value is a text")


@dataclass(frozen=True, slots=True)
class PanelSection:
    """A titled group of a panel's items."""

    title: str
    items: tuple[PanelItem, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))


@dataclass(frozen=True, slots=True)
class Panel:
    """A screen the owner acts on, drawn over the face: the settings, for one. ``name``
    is what the renderer's actions name."""

    name: str
    title: str
    sections: tuple[PanelSection, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "sections", tuple(self.sections))
        if not self.name:
            raise ValueError("a panel needs a name")
        ids = [item.id for section in self.sections for item in section.items]
        if len(ids) != len(set(ids)):
            raise ValueError(f"panel {self.name}: two items have the same id")

    def item(self, id: str) -> PanelItem | None:
        """The item ``id``, if the panel has one."""
        return next(
            (item for section in self.sections for item in section.items if item.id == id), None
        )


@dataclass(frozen=True, slots=True)
class ScreenState:
    """Everything the screen shows; the renderer animates it on its own.

    ``expression`` and ``tone`` name an expression and a colour of the screen protocol
    (protocol/screen/vocabulary.json); ``tone=None`` keeps the expression's own colour.
    ``scene`` names a scene of the protocol, shown with ``scene_data`` (see
    ``scene_data()``) while the face watches from a corner, or is ``None``.
    ``battery`` (percent) and ``charging`` fill the status row, ``battery_low`` shows the
    battery in red (the robot's judgement, from system), and ``muted`` that the
    microphones are off; ``None`` means unknown. ``panel`` is shown over the face, or
    is ``None``. ``asleep``: the robot sleeps, so the face falls asleep and the screen
    may turn off (docs/decisions/0021-sleep-and-wake.md); awake, the screen stays on.
    """

    expression: str = "neutral"
    tone: str | None = None
    scene: str | None = None
    scene_data: Mapping[str, Any] = field(default_factory=dict)
    battery: float | None = None
    charging: bool | None = None
    battery_low: bool | None = None
    muted: bool | None = None
    panel: Panel | None = None
    asleep: bool = False

    def __post_init__(self) -> None:
        if self.battery is not None and not (
            math.isfinite(self.battery) and 0 <= self.battery <= 100
        ):
            raise ValueError(f"battery must be a percentage, got {self.battery!r}")
        object.__setattr__(self, "scene_data", scene_data(self.scene_data))


@dataclass(frozen=True, slots=True)
class Touch:
    """A tap on the screen, as fractions of its width and height from the top left."""

    x: float
    y: float


@dataclass(frozen=True, slots=True)
class Hold:
    """A press held still on the face, where it began (as for ``Touch``)."""

    x: float
    y: float


@dataclass(frozen=True, slots=True)
class Action:
    """The owner acted on ``item`` of ``panel``: its new ``value`` as the item takes it,
    or ``None`` for a button."""

    panel: str
    item: str
    value: Any = None


@dataclass(frozen=True, slots=True)
class Close:
    """The owner closed ``panel``, or left it untouched for a while."""

    panel: str


@dataclass(frozen=True, slots=True)
class Opened:
    """A page that shows the face has connected, and is ready."""


ScreenEvent = Touch | Hold | Action | Close | Opened


class Screen(Port):
    """The screen that shows the robot's face.

    The port carries what to show, not pixels: a renderer draws it and keeps it alive
    (transitions, blinking, glances) without being told, so the software sends only
    changes (docs/decisions/0006-screen-renderer.md). The methods return once the
    change is handed over; they do not wait for it to be drawn.
    """

    @abstractmethod
    async def show(self, state: ScreenState) -> None:
        """Show ``state``; the renderer moves to it with a transition."""

    @abstractmethod
    async def look(self, target: tuple[float, float] | None) -> None:
        """Turn the eyes towards ``target``, or let them roam with ``None``.

        ``x`` and ``y`` go from -1 to 1 across the screen as one faces it: (-1, -1) is
        its top left corner, (0, 0) straight ahead.
        """

    @abstractmethod
    async def voice(self, level: float) -> None:
        """Set the loudness, from 0 to 1, of the voice the face reacts to."""

    @abstractmethod
    async def event(self) -> ScreenEvent:
        """Wait for the next thing someone does on the screen: a tap, a press held, or
        an action on a panel or its closing; or for a page that opened."""
