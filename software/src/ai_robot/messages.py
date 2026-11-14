# SPDX-License-Identifier: MIT
"""Messages exchanged by the modules on the bus.

This file is the contract between modules: they publish and subscribe only to the
classes defined here. Each class is one topic.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Any

from ai_robot.core.bus import Message
from ai_robot.core.checks import finite, positive
from ai_robot.core.data import frozen
from ai_robot.ports.body import BatteryStatus
from ai_robot.ports.cloud import Capability, CloudPart, ConversationAudio, EventKind, RobotPart
from ai_robot.ports.screen import Panel, scene_data


class InteractionState(StrEnum):
    """Where the robot is in a conversation (docs/decisions/0018)."""

    IDLE = "idle"  # in none, or in one that does not listen, between answers
    TRIGGERED = "triggered"  # the robot asked for a conversation, and listens
    ATTENTIVE = "attentive"  # in a conversation, between turns: it listens
    LISTENING = "listening"  # the user speaks to it
    PROCESSING = "processing"  # the user has finished: the answer is coming
    SPEAKING = "speaking"  # an answer plays


@dataclass(frozen=True, slots=True)
class InteractionChanged(Message):
    """Where the conversation is now. Published by voice, on each change."""

    state: InteractionState


class WakeReason(StrEnum):
    """What woke the robot (docs/decisions/0021-sleep-and-wake.md)."""

    TOUCH = "touch"  # a tap, a press held or an action on the screen
    WAKE_WORD = "wake_word"  # its wake word, heard
    PRESENCE = "presence"  # someone came near, as a camera sees
    EVENT = "event"  # its own: a timer, an alarm, a reminder, set-up opening
    APP = "app"  # its owner, from the app


@dataclass(frozen=True, slots=True)
class WakeUp(Message):
    """Something wakes the robot, or tells it that someone is there: asleep, it wakes;
    awake, its countdown to sleep starts again. Published by the module that noticed
    it, read by screen, which decides when the robot sleeps."""

    source: str  # the module's name
    reason: WakeReason


@dataclass(frozen=True, slots=True)
class SoundLevel(Message):
    """Loudness of the latest microphone chunk. Published by voice."""

    rms: float  # root mean square of the 16-bit samples, from 0 to 32768


@dataclass(frozen=True, slots=True)
class BatteryReport(Message):
    """The battery's state. Published by system at a regular interval."""

    status: BatteryStatus
    low: bool | None = None  # whether system judges it low; None: unknown


class ScreenPriority(IntEnum):
    """How much an expression request matters: the screen shows the highest one."""

    AMBIENT = 0  # the robot's mood when nothing else is going on
    INTERACTION = 50  # the conversation: listening, thinking, speaking (asked by screen)
    REACTION = 70  # a short reaction: a smile, surprise at a tap
    ALERT = 90  # something people must notice: a low battery, a fault


@dataclass(frozen=True, slots=True)
class ExpressionRequest(Message):
    """Ask the screen for an expression. Published by any module, read by screen.

    Each ``source`` (by convention, the module's name) holds at most one request: a new
    one replaces it, and ``expression=None`` withdraws it. The screen shows the request
    with the highest ``priority`` that has not expired, the newest among equals, and
    ``neutral`` when there is none. Expressions and tones are named in the screen
    protocol (protocol/screen/vocabulary.json).
    """

    source: str
    expression: str | None
    priority: int = ScreenPriority.REACTION
    duration: float | None = None  # seconds; None: until replaced or withdrawn
    tone: str | None = None  # None: the expression's own colour

    def __post_init__(self) -> None:
        if self.duration is not None:
            positive("duration", self.duration)


@dataclass(frozen=True, slots=True)
class SceneRequest(Message):
    """Ask the screen for a scene. Published by any module, read by screen.

    A scene fills the screen with information (a clock, the weather, a timer…) while
    the face watches from a corner. Requests work as for ``ExpressionRequest``: one per
    ``source``, ``scene=None`` withdraws it, the highest ``priority`` that has not
    expired wins, and with none the face has the screen to itself. Scenes and the
    ``data`` each one takes are listed in the screen protocol
    (protocol/screen/vocabulary.json). ``data`` must convert to JSON, finite numbers
    only; it is kept as a deep, read-only copy (``ports.screen.scene_data``), so that
    no subscriber can change what the others receive.
    """

    source: str
    scene: str | None
    data: Mapping[str, Any] = field(default_factory=dict)
    priority: int = ScreenPriority.REACTION
    duration: float | None = None  # seconds; None: until replaced or withdrawn
    keep_awake: bool = False  # something goes on while it shows: the robot stays awake

    def __post_init__(self) -> None:
        if self.duration is not None:
            positive("duration", self.duration)
        object.__setattr__(self, "data", scene_data(self.data))


@dataclass(frozen=True, slots=True)
class LookAt(Message):
    """Where the eyes should look. Published by vision (planned), read by screen."""

    target: tuple[float, float] | None  # x, y from -1 to 1 across the screen; None: roam

    def __post_init__(self) -> None:
        if self.target is not None:
            x, y = (
                min(1.0, max(-1.0, finite(name, value)))
                for name, value in zip("xy", self.target, strict=True)
            )
            object.__setattr__(self, "target", (x, y))


@dataclass(frozen=True, slots=True)
class ScreenTouched(Message):
    """Someone tapped the screen. Published by screen."""

    x: float  # from 0 at the left edge to 1 at the right one
    y: float  # from 0 at the top to 1 at the bottom


@dataclass(frozen=True, slots=True)
class ScreenHeld(Message):
    """Someone held a press still on the face: the robot opens its settings. Published
    by screen."""

    x: float
    y: float


@dataclass(frozen=True, slots=True)
class PanelRequest(Message):
    """Show ``panel`` over the face, such as the settings, or withdraw it with ``None``.
    Published by the module that describes the panel, read by screen. Each ``source``
    holds at most one panel, and the latest one asked for is shown."""

    source: str
    panel: Panel | None


@dataclass(frozen=True, slots=True)
class PanelAction(Message):
    """The owner acted on ``item`` of ``panel``: its new ``value``, or ``None`` for a
    button. Published by screen."""

    panel: str
    item: str
    value: Any = None


@dataclass(frozen=True, slots=True)
class PanelClosed(Message):
    """The owner closed ``panel``, or left it untouched for a while. Published by
    screen."""

    panel: str


# ---- Capabilities and events: what the cloud can ask of the robot and hear from it ----
# (platform/protocols/cloud/README.md). The cloud module talks to the cloud; the module
# that owns a capability or an event offers it and serves it, so that a new one needs no
# change to the cloud module (requirements EXT-9). ``core.capabilities.CapabilityHost``
# does the bookkeeping for a module.


@dataclass(frozen=True, slots=True)
class CapabilityOffered(Message):
    """A module offers a capability, replacing any earlier one of the same name. Read
    by cloud."""

    owner: str  # the module's name
    capability: Capability


@dataclass(frozen=True, slots=True)
class CapabilityWithdrawn(Message):
    """A module no longer offers a capability. Read by cloud."""

    owner: str
    name: str


@dataclass(frozen=True, slots=True)
class CapabilityCall(Message):
    """Run a capability. Published by cloud once the arguments match the capability's
    input schema; read by the capability's owner, which answers with a
    ``CapabilityResult`` of the same ``call``."""

    call: str  # unique among the calls in progress
    name: str
    arguments: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", frozen("arguments", self.arguments))


@dataclass(frozen=True, slots=True)
class CapabilityResult(Message):
    """The outcome of a ``CapabilityCall``: its ``data``, or an ``error`` the cloud can
    read. Published by the capability's owner, read by cloud."""

    call: str
    data: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", frozen("result data", self.data))


@dataclass(frozen=True, slots=True)
class CapabilityCancelled(Message):
    """The cloud no longer wants the result of a call. Published by cloud; an owner
    running a long action stops it."""

    call: str


@dataclass(frozen=True, slots=True)
class EventOffered(Message):
    """A module can send an event to the cloud. Read by cloud."""

    owner: str
    kind: EventKind


@dataclass(frozen=True, slots=True)
class RobotEvent(Message):
    """An event for the cloud, sent only if the cloud subscribed to it. Published by the
    module that offered it; ``data`` must match its schema."""

    name: str
    data: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", frozen("event data", self.data))


@dataclass(frozen=True, slots=True)
class CloudStatus(Message):
    """Whether a session with the cloud is open, and whether it has conversations: the
    robot offered them and the cloud takes them. Published by cloud on every change."""

    online: bool
    conversations: bool = False


# ---- Conversations (platform/protocols/cloud/README.md, Conversations) ----
# The voice module holds conversations, and the cloud module carries them over the link.
# Each direction is one topic, so that its parts arrive in order: a request, then what
# the robot hears; a start, the turns, and the answers with their text and voice.


@dataclass(frozen=True, slots=True)
class ConversationOffered(Message):
    """A module holds conversations with the cloud: it streams what the robot hears and
    plays the answers, with ``audio``. The link then offers conversations. Published by
    voice, read by cloud."""

    owner: str
    audio: ConversationAudio


@dataclass(frozen=True, slots=True)
class ConversationToCloud(Message):
    """The robot's next part of a conversation: a request for one, what it hears, an
    interrupted answer, its end. Published by voice, read by cloud, which adds the
    robot's context to a request."""

    part: RobotPart


@dataclass(frozen=True, slots=True)
class ConversationFromCloud(Message):
    """The cloud's next part of a conversation: its start and its end, the turns, and
    the answers, with their text and their voice. Published by cloud, read by voice."""

    part: CloudPart


# ---- Set-up: the owner sets the robot up from the app (platform/protocols/setup/README.md) ----
# The setup module holds set-up; the cloud module, which owns the robot's key, signs its
# registration and learns from the server whether the robot is bound.


@dataclass(frozen=True, slots=True)
class BindingChanged(Message):
    """Whether the robot is bound to an owner's account: the server it was registered
    with accepts it. Published by cloud when it starts, then on every change."""

    robot: str  # the robot's identifier
    bound: bool


@dataclass(frozen=True, slots=True)
class RegistrationRequested(Message):
    """The app asks for the robot's registration with ``server``, for ``challenge``
    (platform/protocols/setup/README.md, The registration). Published by setup, read by cloud,
    which answers with a ``RegistrationSigned`` of the same ``request``."""

    request: str  # unique among the requests in progress
    challenge: str
    server: str


@dataclass(frozen=True, slots=True)
class RegistrationSigned(Message):
    """The robot's signed registration, or why there is none: ``error`` is the set-up
    protocol's code, ``server`` (an address the robot does not accept) or ``identity``
    (it could not sign). Published by cloud."""

    request: str
    registration: str | None = None
    error: str | None = None
    detail: str = ""  # for people


@dataclass(frozen=True, slots=True)
class LocaleRequested(Message):
    """The owner asks for these ``languages``, in their order of preference, or this
    ``time_zone``, or both, as the app does at set-up (platform/protocols/setup, ``configure``).
    Published by setup, read by system, which answers with a ``LocaleApplied`` of the
    same ``request``."""

    request: str  # unique among the requests in progress
    languages: tuple[str, ...] | None = None  # BCP 47 tags
    time_zone: str | None = None  # an IANA name

    def __post_init__(self) -> None:
        if self.languages is None and self.time_zone is None:
            raise ValueError("a locale request asks for languages, a time zone, or both")
        if self.languages is not None:
            object.__setattr__(self, "languages", tuple(self.languages))


@dataclass(frozen=True, slots=True)
class LocaleApplied(Message):
    """What the robot keeps after a ``LocaleRequested``: the languages asked for that it
    supports, in their order, or its own if it supports none of them; and its time zone.
    ``error`` is set-up's code ``time_zone`` when it could not take the zone asked for.
    Published by system."""

    request: str
    languages: tuple[str, ...]
    time_zone: str
    error: str | None = None
    detail: str = ""  # for people

    def __post_init__(self) -> None:
        object.__setattr__(self, "languages", tuple(self.languages))


@dataclass(frozen=True, slots=True)
class LocaleChanged(Message):
    """The robot's languages, in the owner's order, and its time zone (ai-platform
    ADR-0002). Published by system when it starts, then on every change."""

    languages: tuple[str, ...]  # BCP 47 tags; the first is the one it shows
    time_zone: str  # an IANA name

    def __post_init__(self) -> None:
        if not self.languages:
            raise ValueError("the robot has at least one language")
        object.__setattr__(self, "languages", tuple(self.languages))


@dataclass(frozen=True, slots=True)
class MicrophonesChanged(Message):
    """Whether the microphones are on: off, the robot hears nothing, and the screen says
    so. Published by voice when it starts, then on every change."""

    on: bool


# ---- Settings: what the owner sees and changes on the robot's touch screen ----
# (docs/decisions/0016-settings.md). Each module offers its own settings, and applies
# them; the settings module keeps what the owner chose, and draws the panel.

SETTING_NAME = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+")  # owner.setting


class SettingKind(StrEnum):
    VALUE = "value"  # a value the owner chooses, as its schema allows
    ACTION = "action"  # something the owner does, such as opening set-up
    INFO = "info"  # a line to read


class SettingSection(StrEnum):
    """Where a setting shows: the sections, in this order, with their titles."""

    NETWORK = "Wi-Fi and app"
    SOUND = "Sound"
    DISPLAY = "Display"
    LOCALE = "Language and time"
    CLOUD = "What the cloud may do"
    ABOUT = "About"


def _plain_value(name: str, value: Any) -> Any:
    """``value`` if it is a JSON scalar: a boolean, a finite number, a text or none."""
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"setting {name}: its value must be finite, got {value!r}")
    if value is not None and not isinstance(value, bool | int | float | str):
        raise ValueError(f"setting {name}: its value must be plain JSON, got {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class Setting:
    """Something the owner sees or changes in the robot's settings.

    ``name`` starts with the owner module's name (``voice.volume``). A ``VALUE`` has a
    JSON Schema for its value, ``schema``: a boolean; a number with a ``minimum`` and a
    ``maximum``, by steps of ``multipleOf``; or one of ``oneOf``'s constants, each with a
    ``title``. ``value`` is a ``VALUE``'s current value, or an ``INFO``'s text; an
    ``ACTION`` has none.
    """

    name: str
    kind: SettingKind
    section: SettingSection
    title: str
    value: Any = None
    schema: Mapping[str, Any] | None = None
    help: str | None = None
    unit: str | None = None  # shown after a number, such as "%"
    confirm: str | None = None  # asked before a boolean is switched on, or an action runs
    closes: bool = False  # an action that leaves the settings, such as opening set-up

    def __post_init__(self) -> None:
        if not SETTING_NAME.fullmatch(self.name):
            raise ValueError(f"a setting's name is owner.name, in lower case: {self.name!r}")
        if not self.title:
            raise ValueError(f"setting {self.name} needs a title")
        _plain_value(self.name, self.value)
        if (self.kind is SettingKind.VALUE) != (self.schema is not None):
            raise ValueError(f"setting {self.name}: a value, and only a value, has a schema")
        if self.schema is not None:
            object.__setattr__(self, "schema", frozen(f"{self.name} schema", self.schema))
        if self.kind is SettingKind.ACTION and self.value is not None:
            raise ValueError(f"setting {self.name}: an action has no value")
        if self.kind is SettingKind.INFO and not isinstance(self.value, str):
            raise ValueError(f"setting {self.name}: an info's value is its text")
        if self.closes and self.kind is not SettingKind.ACTION:
            raise ValueError(f"setting {self.name}: only an action closes the settings")


@dataclass(frozen=True, slots=True)
class SettingOffered(Message):
    """A module offers a setting, or tells its new value or text. Read by settings."""

    owner: str  # the module's name
    setting: Setting


@dataclass(frozen=True, slots=True)
class SettingChanged(Message):
    """The owner changed setting ``name`` to ``value``, or pressed it if it is an action
    (``value`` is then ``None``). Published by settings, from the screen or from what
    it kept; read by the setting's module, which checks the value and applies it."""

    name: str
    value: Any = None

    def __post_init__(self) -> None:
        _plain_value(self.name, self.value)


@dataclass(frozen=True, slots=True)
class SettingRequested(Message):
    """The owner chose ``value`` for setting ``name`` somewhere other than the panel,
    such as in the app at set-up. The settings module keeps it and passes it on, as it
    does a choice made on the panel. Published by the module that heard it, read by
    settings."""

    name: str
    value: Any

    def __post_init__(self) -> None:
        _plain_value(self.name, self.value)


@dataclass(frozen=True, slots=True)
class SettingsOpened(Message):
    """The settings are on screen: modules refresh what their lines show. Published by
    settings."""
