# SPDX-License-Identifier: MIT
"""The link to the cloud, ``ai-cloud`` (platform/protocols/cloud/README.md)."""

from __future__ import annotations

import re
import urllib.parse
from abc import abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ai_robot.core.data import Schema, frozen
from ai_robot.ports.audio import AudioFormat
from ai_robot.ports.base import Port
from ai_robot.ports.identity import Identity

# Names of capabilities and events, as MCP allows for tool names.
NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")
LOOPBACK = {"127.0.0.1", "::1", "localhost"}  # where plain http:// and ws:// may go (SEC-1)


def check_url(name: str, url: str, schemes: tuple[str, str]) -> str:
    """``url`` if its scheme is the secure one of ``schemes``, or the other one to this
    computer only (SEC-1); otherwise ``ValueError``."""
    secure, plain = schemes
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in schemes or not parts.hostname:
        raise ValueError(f"{name} must be a {secure}:// address, got {url!r}")
    if parts.scheme == plain and parts.hostname not in LOOPBACK:
        raise ValueError(
            f"{name}: {plain}:// is allowed only to this computer (127.0.0.1, ::1, "
            f"localhost); use {secure}://, got {url!r}"
        )
    return url


class SafetyClass(StrEnum):
    """How much a capability can affect people, which the robot enforces itself
    whatever the cloud asks (platform/protocols/cloud/requirements.md, SEC-8 and SEC-9)."""

    INFORMATIONAL = "informational"  # the screen, sounds
    PRIVACY = "privacy"  # the camera; a conversation the cloud opens to listen
    PHYSICAL = "physical"  # motion
    SYSTEM = "system"  # reboot, update, settings


def _name(kind: str, name: str) -> str:
    if not (isinstance(name, str) and NAME.fullmatch(name)):
        raise ValueError(
            f"{kind} name must be 1 to 128 letters, digits, '_', '.' or '-', got {name!r}"
        )
    return name


@dataclass(frozen=True, slots=True)
class Capability:
    """Something the robot does when the cloud asks: an MCP tool.

    ``name`` starts with the owning module's name (``screen.show_scene``). The schemas
    are JSON Schema 2020-12, kept as deep, read-only copies: ``input_schema`` describes
    the arguments, an object; ``output_schema``, if any, the result's data. ``version``
    changes when the meaning does (EXT-7).
    """

    name: str
    description: str
    input_schema: Mapping[str, Any]
    safety: SafetyClass
    version: int = 1
    output_schema: Mapping[str, Any] | None = None
    title: str | None = None

    def __post_init__(self) -> None:
        _name("capability", self.name)
        object.__setattr__(self, "safety", SafetyClass(self.safety))
        if isinstance(self.version, bool) or not (
            isinstance(self.version, int) and self.version >= 1
        ):
            raise ValueError(f"version must be a whole number from 1, got {self.version!r}")
        schema = frozen(f"{self.name} input_schema", self.input_schema)
        if schema.get("type") != "object":
            raise ValueError(f"{self.name} input_schema must describe an object")
        Schema(f"{self.name} input_schema", schema)
        object.__setattr__(self, "input_schema", schema)
        if self.output_schema is not None:
            output = frozen(f"{self.name} output_schema", self.output_schema)
            Schema(f"{self.name} output_schema", output)
            object.__setattr__(self, "output_schema", output)


@dataclass(frozen=True, slots=True)
class EventKind:
    """An event the robot can send to the cloud: its name and the JSON Schema of its
    data (EXT-6)."""

    name: str
    description: str
    schema: Mapping[str, Any] = field(default_factory=lambda: {"type": "object"})

    def __post_init__(self) -> None:
        _name("event", self.name)
        schema = frozen(f"{self.name} schema", self.schema)
        Schema(f"{self.name} schema", schema)
        object.__setattr__(self, "schema", schema)


@dataclass(frozen=True, slots=True)
class ToolCall:
    """The cloud asks the robot to run a capability."""

    id: str  # unique among the calls the link has received
    name: str
    arguments: Mapping[str, Any]  # not checked yet against the capability's input schema

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", frozen("arguments", self.arguments))


@dataclass(frozen=True, slots=True)
class CallCancelled:
    """The cloud no longer wants the result of a call."""

    id: str


# ---- Conversations (platform/protocols/cloud/README.md, Conversations) ----
# The module that holds conversations (voice) and the link exchange their parts. Each
# names its conversation, so that a part that crosses the conversation's end is known.


@dataclass(frozen=True, slots=True)
class ConversationAudio:
    """The audio a module that holds conversations exchanges with the link: the format
    of what the robot hears, which it gives, and of the voice it plays. The link converts
    to and from what travels, Opus over WebRTC (platform/protocols/cloud/README.md, The
    room)."""

    speech: AudioFormat
    voice: AudioFormat


class Trigger(StrEnum):
    """What opened a conversation, on the robot."""

    WAKE_WORD = "wake_word"
    TOUCH = "touch"
    BUTTON = "button"


@dataclass(frozen=True, slots=True)
class ConversationRequest:
    """Someone spoke to the robot, which asks the cloud for a conversation.
    ``conversation`` is the robot's identifier for it, a UUID. ``context`` is a snapshot
    of the robot's situation (IO-4), which the cloud module fills in. What the robot
    hears from then on follows as ``Speech``: the link keeps it until the cloud
    listens."""

    conversation: str
    trigger: Trigger
    context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "trigger", Trigger(self.trigger))
        object.__setattr__(self, "context", frozen("context", self.context))


@dataclass(frozen=True, slots=True)
class ConversationStart:
    """A conversation starts: the one the robot asked for, or one the cloud opens and
    the link accepted. With ``listen``, the robot streams what it hears; without, it
    only plays the cloud's voice."""

    conversation: str
    listen: bool


@dataclass(frozen=True, slots=True)
class ConversationEnd:
    """A conversation is over, for ``reason`` (platform/protocols/cloud/README.md,
    Ending): the robot ends it, or the link says that it ended, or could not start
    (then with the cloud's error, such as ``limit_reached``)."""

    conversation: str
    reason: str


@dataclass(frozen=True, slots=True)
class Speech:
    """What the robot hears in a conversation: PCM in the conversation's speech format."""

    conversation: str
    pcm: bytes


@dataclass(frozen=True, slots=True)
class TurnStart:
    """The cloud heard the user start speaking: a turn begins."""

    conversation: str
    turn: str


@dataclass(frozen=True, slots=True)
class SpeechEnd:
    """The cloud judges that the user has finished speaking."""

    conversation: str
    turn: str


@dataclass(frozen=True, slots=True)
class TurnCancel:
    """The cloud dropped a turn, for ``reason``: noise, or speech not meant for the
    robot."""

    conversation: str
    turn: str
    reason: str


@dataclass(frozen=True, slots=True)
class ResponseStart:
    """An answer begins, in ``language``. ``turn`` is the turn it answers, or ``None``
    for an answer that no turn asked for, such as a reminder."""

    conversation: str
    response: str
    turn: str | None
    language: str


@dataclass(frozen=True, slots=True)
class ResponseText:
    """A piece of the text of an answer's speech."""

    conversation: str
    response: str
    delta: str


@dataclass(frozen=True, slots=True)
class Voice:
    """A piece of the cloud's voice, as it plays: PCM in the conversation's voice
    format."""

    conversation: str
    pcm: bytes


@dataclass(frozen=True, slots=True)
class ResponseEnd:
    """The answer is complete: no more of it will come."""

    conversation: str
    response: str


@dataclass(frozen=True, slots=True)
class ResponseStop:
    """Stop playing an answer at once, and drop what is left of it."""

    conversation: str
    response: str


@dataclass(frozen=True, slots=True)
class Interrupted:
    """The user spoke over an answer: the robot stopped playing it."""

    conversation: str
    response: str


# The robot's part of a conversation, and the cloud's, each in the order it happens.
RobotPart = ConversationRequest | Speech | Interrupted | ConversationEnd
CloudPart = (
    ConversationStart
    | ConversationEnd
    | TurnStart
    | SpeechEnd
    | TurnCancel
    | ResponseStart
    | ResponseText
    | Voice
    | ResponseEnd
    | ResponseStop
)


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """What the robot tells the cloud of itself (platform/protocols/cloud/README.md,
    ``hello``): its kind and model, its languages in its owner's order, its time zone (an
    IANA name), and the safety classes its owner allows (SEC-9)."""

    kind: str
    model: str
    languages: tuple[str, ...]
    time_zone: str
    allowed_classes: frozenset[SafetyClass]

    def __post_init__(self) -> None:
        if not self.languages:
            raise ValueError("the robot has at least one language")
        object.__setattr__(self, "languages", tuple(self.languages))
        allowed = frozenset(SafetyClass(name) for name in self.allowed_classes)
        object.__setattr__(self, "allowed_classes", allowed)


@dataclass(frozen=True, slots=True)
class LinkStatus:
    """Whether a session with the cloud is open, which events it subscribed to, and
    whether it has conversations: the robot offered them and the cloud takes them.

    ``bound`` is what the server last said of the robot when it asked for its tokens:
    ``True`` if it accepts it, ``False`` if it does not know it or revoked it (the robot
    is not set up), ``None`` if it has not said yet.

    ``update_required``: the cloud no longer speaks the robot's version of the link, or
    no longer serves its software. The robot needs an update, and the link tries again
    only now and then."""

    online: bool
    session: str | None = None
    events: frozenset[str] = frozenset()
    conversations: bool = False
    bound: bool | None = None
    update_required: bool = False


class Cloud(Port):
    """The link to the cloud. The cloud drives the robot through the capabilities the
    robot offers, receives the events it subscribed to, and holds conversations with
    it: the robot streams what it hears, and the cloud takes the turns and streams the
    robot's answers.

    Like every port, it inherits ``open`` and ``close`` from ``Port``, and the
    application calls them:
    - ``open``, before the modules start, only prepares the link. The ``websocket``
      adapter loads the protocol's schema and starts the task that connects; the
      ``sim`` adapter needs nothing, so it keeps ``Port``'s empty ``open``.
    - ``close``, after the modules stop, ends the session.

    The link opens no session until ``describe`` and ``offer`` have both been called, so
    that the first session already says what the robot is and everything it can do. It
    reconnects on its own after a loss; ``status`` tells when the session opens and
    closes, and what the server says of the robot.
    """

    @abstractmethod
    async def identify(self, identity: Identity) -> None:
        """Prove that the robot is ``identity`` when the link asks the server for its
        tokens (docs/decisions/0010-device-identity.md). Called before ``offer``."""

    @abstractmethod
    async def serve(self, server: str) -> None:
        """Ask ``server`` for the robot's tokens from now on: the ``ai-cloud`` the
        robot was registered with at set-up (platform/protocols/setup/README.md). Until then, the
        link asks the server of its own settings. ``ValueError`` for an address the link
        does not accept."""

    @abstractmethod
    async def describe(self, device: DeviceInfo) -> None:
        """Say what the robot is, and what its owner chose. A change reaches the cloud
        at once, if a session is open, and the next session in any case."""

    @abstractmethod
    async def listening(self, allowed: bool) -> None:
        """Say whether the robot may stream what it hears: its microphones are on. While
        it may not, a conversation the cloud opens to listen is refused. Until it is
        called, the robot may."""

    @abstractmethod
    async def offer(
        self,
        capabilities: Sequence[Capability],
        events: Sequence[EventKind],
        *,
        conversation: ConversationAudio | None = None,
    ) -> None:
        """Set what the robot offers. ``conversation``, if any, offers conversations: a
        module holds them, with this audio. A change of ``capabilities`` reaches the cloud
        at once; a change of ``events`` or ``conversation``, at the next session."""

    @abstractmethod
    async def request(self) -> ToolCall | CallCancelled:
        """Wait for the next call from the cloud, or the cancellation of one."""

    @abstractmethod
    async def reply(
        self,
        call: ToolCall,
        *,
        data: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        """Answer ``call`` with the result's ``data``, or with an ``error`` the cloud can
        read. A reply after the session ended is dropped."""

    @abstractmethod
    async def emit(self, name: str, data: Mapping[str, Any]) -> None:
        """Send an event, if a session is open and the cloud subscribed to it."""

    @abstractmethod
    async def status(self) -> LinkStatus:
        """Wait for the next change of the link's status."""

    @abstractmethod
    async def converse(self, part: RobotPart) -> None:
        """Send the robot's next part of a conversation. A request the link cannot carry,
        with no session or one without conversations, comes back at once from
        ``conversation`` as a ``ConversationEnd``; speech for a conversation that is
        over is dropped."""

    @abstractmethod
    async def conversation(self) -> CloudPart:
        """Wait for the cloud's next part of a conversation. A conversation that ends,
        whatever ends it, ends with a ``ConversationEnd``."""

    @abstractmethod
    async def leave(self) -> bool:
        """Leave the robot's home before its factory reset (platform/protocols/cloud/
        README.md, ``leave``; docs/decisions/0023-factory-reset.md). The conversation in
        progress, if any, ends first, and the robot holds no other from then on.

        Returns ``True`` once the robot is out of its home, or was in none; ``False``
        when no outcome came in time, or the cloud cannot take a leave: the app may
        still list the robot."""
