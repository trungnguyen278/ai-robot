# SPDX-License-Identifier: MIT
"""Screen: what the robot shows on its display."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar

from ai_robot.core.bus import Message, Subscription
from ai_robot.core.capabilities import CapabilityHost
from ai_robot.core.checks import finite, positive
from ai_robot.core.config import protocol_dir
from ai_robot.core.module import Module, ModuleContext
from ai_robot.core.settings import SettingsHost
from ai_robot.messages import (
    BatteryReport,
    ExpressionRequest,
    InteractionChanged,
    InteractionState,
    LookAt,
    MicrophonesChanged,
    PanelAction,
    PanelClosed,
    PanelRequest,
    SceneRequest,
    ScreenHeld,
    ScreenPriority,
    ScreenTouched,
    Setting,
    SettingKind,
    SettingSection,
    SoundLevel,
    WakeReason,
    WakeUp,
)
from ai_robot.ports import (
    Action,
    Capability,
    Close,
    DisplayPower,
    EventKind,
    Hold,
    Opened,
    Panel,
    SafetyClass,
    Screen,
    ScreenState,
    Touch,
)

M = TypeVar("M", bound=Message)
T = TypeVar("T")

DEFAULT_EXPRESSION = "neutral"

# What the face shows during a conversation; None: nothing in particular.
INTERACTION_EXPRESSIONS: dict[InteractionState, str | None] = {
    InteractionState.IDLE: None,
    InteractionState.TRIGGERED: "listening",
    InteractionState.ATTENTIVE: "listening",
    InteractionState.LISTENING: "listening",
    InteractionState.PROCESSING: "thinking",
    InteractionState.SPEAKING: "speaking",
}
# The states in which the robot streams what it hears, and the face follows the voice.
LISTENING_STATES = frozenset(
    {InteractionState.TRIGGERED, InteractionState.ATTENTIVE, InteractionState.LISTENING}
)
INTERACTION_SOURCE = "interaction"  # the source of the requests made for the conversation

# The sources of the cloud's requests.
CLOUD_REACTION = "cloud.reaction"
CLOUD_MOOD = "cloud.mood"
CLOUD_SCENE = "cloud.scene"

SLEEP_SETTING = "screen.sleep_after"
NEVER = 0  # the "Sleep after" that never sleeps
SECONDS_PER_MINUTE = 60

TOUCHED = EventKind(
    "screen.touched",
    "Someone tapped the screen",
    {
        "type": "object",
        "properties": {
            "x": {"type": "number", "minimum": 0, "maximum": 1, "description": "From the left"},
            "y": {"type": "number", "minimum": 0, "maximum": 1, "description": "From the top"},
        },
        "required": ["x", "y"],
        "additionalProperties": False,
    },
)


def load_vocabulary(path: Path | None = None) -> Mapping[str, Any]:
    """The screen protocol's vocabulary (protocol/screen/vocabulary.json)."""
    path = protocol_dir() / "screen" / "vocabulary.json" if path is None else path
    return json.loads(path.read_text(encoding="utf-8"))


def _showing(kind: str, nullable: bool = False) -> dict[str, Any]:
    showing: dict[str, Any] = {
        "type": ["string", "null"] if nullable else "string",
        "description": f"The {kind} the screen shows now; a more important one may hide yours",
    }
    return {
        "type": "object",
        "properties": {"showing": showing},
        "required": ["showing"],
        "additionalProperties": False,
    }


def _data_schema(fields: Mapping[str, Any]) -> dict[str, Any]:
    """The JSON Schema of a scene's data, from its fields in the vocabulary."""
    properties: dict[str, Any] = {}
    for name, field in fields.items():
        prop: dict[str, Any] = {"type": field["type"]}
        if "values" in field:
            prop["enum"] = list(field["values"])
        if "max_length" in field:
            prop["maxLength"] = field["max_length"]
        if "description" in field:
            prop["description"] = field["description"]
        properties[name] = prop
    return {"type": "object", "properties": properties, "additionalProperties": False}


def capabilities(
    vocabulary: Mapping[str, Any],
    *,
    reaction_seconds: float,
    reaction_max_seconds: float,
    scene_max_seconds: float,
) -> list[Capability]:
    """What the cloud can ask of the screen, from the screen protocol's vocabulary, so
    that the cloud sees exactly the expressions, colours and scenes the renderer draws,
    but the robot's own scenes (``robot_only``, such as set-up). A reaction lasts
    ``reaction_seconds`` unless the cloud says, up to ``reaction_max_seconds``; a scene,
    up to ``scene_max_seconds``."""
    expressions = dict(vocabulary["expressions"])
    looks = "; ".join(f"{name}: {look}" for name, look in expressions.items())
    tone = {
        "type": "string",
        "enum": list(vocabulary["tones"]),
        "description": "A colour; the expression's own when missing",
    }
    scenes = {
        name: scene
        for name, scene in vocabulary["scenes"].items()
        if not scene.get("robot_only", False)
    }
    contents = "; ".join(f"{name}: {scene['description']}" for name, scene in scenes.items())
    return [
        Capability(
            name="screen.show_expression",
            title="Show an expression",
            description=(
                "Show an expression on the robot's face for a few seconds, as a reaction; "
                "the face then goes back to what it showed before."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "enum": list(expressions),
                        "description": looks,
                    },
                    "seconds": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "maximum": reaction_max_seconds,
                        "description": f"How long; {reaction_seconds:g} when missing",
                    },
                    "tone": tone,
                },
                "required": ["expression"],
                "additionalProperties": False,
            },
            output_schema=_showing("expression"),
            safety=SafetyClass.INFORMATIONAL,
        ),
        Capability(
            name="screen.set_mood",
            title="Set the mood",
            description=(
                "Set the robot's lasting mood: an expression shown whenever nothing more "
                "important is on the face, until it is changed. null clears it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "expression": {
                        "type": ["string", "null"],
                        "enum": [*expressions, None],
                        "description": looks,
                    },
                    "tone": tone,
                },
                "required": ["expression"],
                "additionalProperties": False,
            },
            output_schema=_showing("expression"),
            safety=SafetyClass.INFORMATIONAL,
        ),
        Capability(
            name="screen.show_scene",
            title="Show a scene",
            description=(
                "Fill the screen with a scene while the face watches from a corner. "
                "null removes the scene the cloud asked for."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "scene": {
                        "type": ["string", "null"],
                        "enum": [*scenes, None],
                        "description": contents,
                    },
                    "data": {"type": "object", "description": "What the scene shows"},
                    "seconds": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "maximum": scene_max_seconds,
                        "description": "How long; until replaced or removed when missing",
                    },
                },
                "required": ["scene"],
                "additionalProperties": False,
                "allOf": [
                    {
                        "if": {"properties": {"scene": {"const": name}}},
                        "then": {"properties": {"data": _data_schema(scene["data"])}},
                    }
                    for name, scene in scenes.items()
                ],
            },
            output_schema=_showing("scene", nullable=True),
            safety=SafetyClass.INFORMATIONAL,
        ),
    ]


@dataclass(frozen=True, slots=True)
class Face:
    expression: str
    tone: str | None = None


@dataclass(frozen=True, slots=True)
class Scene:
    name: str
    data: Mapping[str, Any]
    keep_awake: bool = False


@dataclass(frozen=True, slots=True)
class Request(Generic[T]):
    content: T
    priority: int
    expires: float | None  # event-loop time; None: until replaced or withdrawn
    order: int  # arrival order: the newest wins among equal priorities


class Arbiter(Generic[T]):
    """Chooses what to show from the requests of several sources.

    Each source holds at most one request. The highest priority that has not expired
    wins, the newest among equals: the multiplexer pattern of ROS's twist_mux, also how
    OpenVoiceOS stacks what its screen shows.
    """

    def __init__(self) -> None:
        self._requests: dict[str, Request[T]] = {}
        self._order = 0

    def put(
        self, source: str, content: T | None, *, priority: int, expires: float | None = None
    ) -> None:
        """Record ``source``'s request, replacing its previous one; ``None`` withdraws it."""
        if content is None:
            self._requests.pop(source, None)
            return
        self._order += 1
        self._requests[source] = Request(content, priority, expires, self._order)

    def winner(self, now: float) -> Request[T] | None:
        """The request to show at ``now``; expired ones are dropped."""
        return max(self.active(now), key=lambda r: (r.priority, r.order), default=None)

    def active(self, now: float) -> list[Request[T]]:
        """The requests that have not expired at ``now``; expired ones are dropped."""
        for source, request in list(self._requests.items()):
            if request.expires is not None and request.expires <= now:
                del self._requests[source]
        return list(self._requests.values())

    def next_expiry(self) -> float | None:
        times = [r.expires for r in self._requests.values() if r.expires is not None]
        return min(times, default=None)


class ScreenModule(Module):
    """Decides what the screen shows; the renderer draws it and keeps it alive.

    - Expressions: other modules ask with ``ExpressionRequest``; the conversation
      (``InteractionChanged``) asks on its own at ``ScreenPriority.INTERACTION``.
      An ``Arbiter`` picks one, ``neutral`` when nothing is asked.
    - Scenes: other modules ask with ``SceneRequest``, and a second ``Arbiter`` picks
      one. The face keeps its expression while it watches a scene from a corner.
    - The status row: the battery, from ``BatteryReport``.
    - The gaze (``LookAt``) and, while the robot listens, the microphone's loudness
      go straight to the renderer.
    - Taps on the screen are published as ``ScreenTouched``, and sent to the cloud as
      the event ``screen.touched``; a press held still, as ``ScreenHeld``.
    - Panels, such as the settings: modules ask with ``PanelRequest``, and the latest
      one asked for is shown over the face. What the owner does on it is published as
      ``PanelAction`` and ``PanelClosed``.
    - The status row also shows when the microphones are off (``MicrophonesChanged``).
    - The cloud can ask for an expression, a mood or a scene (``capabilities``): a
      reaction, at ``ScreenPriority.REACTION``; a mood, at ``AMBIENT``; a scene, at
      ``REACTION``.

    - Sleep (docs/decisions/0021-sleep-and-wake.md): after "Sleep after" minutes
      with nothing going on, the robot falls asleep: the face closes its eyes, and the
      screen turns off. Nothing counts while something goes on: a conversation, a
      scene that keeps it awake (``keep_awake``, such as set-up's code or a timer), or
      a panel; the count starts again when it ends, and a hold that starts while the
      robot sleeps wakes it. ``WakeUp`` wakes the robot, or starts the count again.
      Each tap, hold and action publishes one; a tap on a sleeping robot only wakes it.
      ``display_power`` lights a dark screen when the robot wakes from anything but a
      touch, and when a page opens while the robot is awake.

    The renderer animates on its own (transitions, blinking, glances, its start-up),
    so this module sends only changes.
    """

    owns = ("screen", "display_power")

    def __init__(
        self,
        ctx: ModuleContext,
        *,
        quiet_db: float,
        loud_db: float,
        voice_rate: float,
        reaction_seconds: float,
        reaction_max_seconds: float,
        scene_max_seconds: float,
        sleep_after: float,
        sleep_choices: list[float],
    ) -> None:
        super().__init__(ctx)
        # The microphone's loudness, in dBFS, that maps to a voice level of 0 and of 1.
        self.quiet_db = finite("quiet_db", quiet_db)
        self.loud_db = finite("loud_db", loud_db)
        if self.loud_db <= self.quiet_db:
            raise ValueError("loud_db must be above quiet_db")
        # Seconds between two voice levels sent to the screen.
        self.voice_interval = 1 / positive("voice_rate", voice_rate)
        self.reaction_seconds = positive("reaction_seconds", reaction_seconds)
        self.reaction_max_seconds = positive("reaction_max_seconds", reaction_max_seconds)
        if self.reaction_seconds > self.reaction_max_seconds:
            raise ValueError("reaction_seconds must not exceed reaction_max_seconds")
        self.scene_max_seconds = positive("scene_max_seconds", scene_max_seconds)
        self.screen = self.port("screen", Screen)
        self.display = self.port("display_power", DisplayPower)
        if not sleep_choices or len(set(sleep_choices)) != len(sleep_choices):
            raise ValueError("sleep_choices must list distinct numbers of minutes")
        for minutes in sleep_choices:
            positive("sleep_choices", minutes)
        if sleep_after != NEVER and sleep_after not in sleep_choices:
            raise ValueError("sleep_after must be one of sleep_choices, or 0 for never")
        self.sleep_after = sleep_after  # minutes; NEVER: it does not sleep
        self.asleep = False
        self._last_activity = 0.0  # event-loop time when the count started
        self._held = False  # something goes on that keeps the robot awake
        self._activity = asyncio.Event()  # the count started again, or stopped
        self._interaction = InteractionState.IDLE
        self._keeps_awake: frozenset[str] = frozenset()  # the cloud's scenes that do
        self.settings = SettingsHost(self)
        choices = [{"const": m, "title": _minutes(m)} for m in sorted(sleep_choices)]
        self.settings.add(
            Setting(
                SLEEP_SETTING,
                SettingKind.VALUE,
                SettingSection.DISPLAY,
                "Sleep after",
                sleep_after,
                schema={"oneOf": [*choices, {"const": NEVER, "title": "Never"}]},
                help="With nothing going on for this long, it sleeps and its screen turns off",
            ),
            self._set_sleep_after,
        )
        self.faces: Arbiter[Face] = Arbiter()
        self.scenes: Arbiter[Scene] = Arbiter()
        self.state: ScreenState | None = None  # what the screen was last given
        self.listening = False
        self._battery: float | None = None
        self._charging: bool | None = None
        self._battery_low: bool | None = None
        self._muted: bool | None = None
        self._panels: dict[str, tuple[int, Panel]] = {}  # source -> (order asked, panel)
        self._panel_order = 0
        self._level = 0.0
        self._level_sent_at = -math.inf
        self._changed = asyncio.Event()
        self.subscriptions: list[Subscription[Any]] = []
        self.expression_requests = self._subscribe(ExpressionRequest)
        self.scene_requests = self._subscribe(SceneRequest)
        self.interaction = self._subscribe(InteractionChanged)
        self.batteries = self._subscribe(BatteryReport)
        self.levels = self._subscribe(SoundLevel)
        self.gazes = self._subscribe(LookAt)
        self.panel_requests = self._subscribe(PanelRequest)
        self.microphones = self._subscribe(MicrophonesChanged)
        self.wakes = self._subscribe(WakeUp)
        self.capabilities = CapabilityHost(self)

    def _subscribe(self, kind: type[M]) -> Subscription[M]:
        subscription = self.bus.subscribe(kind)
        self.subscriptions.append(subscription)
        return subscription

    async def start(self) -> None:
        self._last_activity = self._now()
        await self._refresh()
        vocabulary = await asyncio.to_thread(load_vocabulary)
        self._keeps_awake = frozenset(
            name for name, scene in vocabulary["scenes"].items() if scene.get("keeps_awake")
        )
        offered = capabilities(
            vocabulary,
            reaction_seconds=self.reaction_seconds,
            reaction_max_seconds=self.reaction_max_seconds,
            scene_max_seconds=self.scene_max_seconds,
        )
        handlers = (self._show_expression, self._set_mood, self._show_scene)
        for capability, handler in zip(offered, handlers, strict=True):
            self.capabilities.add(capability, handler)
        self.capabilities.add_event(TOUCHED)
        self.capabilities.offer()
        self.settings.offer()

    async def run(self) -> None:
        await self.settings.apply_pending()
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self._follow(self.expression_requests, self._on_expression))
            tasks.create_task(self._follow(self.scene_requests, self._on_scene))
            tasks.create_task(self._follow(self.interaction, self._on_interaction))
            tasks.create_task(self._follow(self.batteries, self._on_battery))
            tasks.create_task(self._follow(self.levels, self._on_level))
            tasks.create_task(self._follow(self.gazes, self._on_gaze))
            tasks.create_task(self._follow(self.panel_requests, self._on_panel))
            tasks.create_task(self._follow(self.microphones, self._on_microphones))
            tasks.create_task(self._follow(self.wakes, self._on_wake))
            tasks.create_task(self._fall_asleep())
            tasks.create_task(self.settings.serve())
            tasks.create_task(self._expire())
            tasks.create_task(self._forward_events())
            tasks.create_task(self.capabilities.serve())

    async def stop(self) -> None:
        for subscription in self.subscriptions:
            subscription.close()
        self.capabilities.close()
        self.settings.close()

    @staticmethod
    async def _follow(
        subscription: Subscription[M], handle: Callable[[M], Awaitable[None]]
    ) -> None:
        async for message in subscription:
            await handle(message)

    def _expires(self, duration: float | None) -> float | None:
        return None if duration is None else self._now() + duration

    async def _on_expression(self, request: ExpressionRequest) -> None:
        face = None if request.expression is None else Face(request.expression, request.tone)
        self.faces.put(
            request.source, face, priority=request.priority, expires=self._expires(request.duration)
        )
        await self._refresh()

    async def _on_scene(self, request: SceneRequest) -> None:
        scene = (
            None
            if request.scene is None
            else Scene(request.scene, request.data, request.keep_awake)
        )
        self.scenes.put(
            request.source,
            scene,
            priority=request.priority,
            expires=self._expires(request.duration),
        )
        await self._refresh()

    async def _on_interaction(self, message: InteractionChanged) -> None:
        self._interaction = message.state
        self.listening = message.state in LISTENING_STATES
        expression = INTERACTION_EXPRESSIONS[message.state]
        face = None if expression is None else Face(expression)
        self.faces.put(INTERACTION_SOURCE, face, priority=ScreenPriority.INTERACTION)
        if not self.listening and self._level > 0:
            await self._send_level(0.0)
        await self._refresh()

    async def _on_battery(self, report: BatteryReport) -> None:
        percent = report.status.percent
        self._battery = None if percent is None else min(100.0, max(0.0, percent))
        self._charging = report.status.charging
        self._battery_low = report.low
        await self._refresh()

    async def _on_level(self, message: SoundLevel) -> None:
        if self.listening and self._now() - self._level_sent_at >= self.voice_interval:
            await self._send_level(self.loudness(message.rms))

    async def _on_gaze(self, message: LookAt) -> None:
        await self.screen.look(message.target)

    async def _on_panel(self, request: PanelRequest) -> None:
        if request.panel is None:
            self._panels.pop(request.source, None)
        else:
            self._panel_order += 1
            self._panels[request.source] = (self._panel_order, request.panel)
        # Sent even when unchanged: the renderer shows the owner's changes at once, and a
        # panel asked for again puts back one the robot refused.
        await self._refresh(always=True)

    async def _on_microphones(self, message: MicrophonesChanged) -> None:
        self._muted = not message.on
        await self._refresh()

    def loudness(self, rms: float) -> float:
        """The voice level, from 0 to 1, of a microphone chunk of loudness ``rms``."""
        if rms <= 0:
            return 0.0
        db = 20 * math.log10(rms / 32768)
        return min(1.0, max(0.0, (db - self.quiet_db) / (self.loud_db - self.quiet_db)))

    async def _send_level(self, level: float) -> None:
        self._level = level
        self._level_sent_at = self._now()
        await self.screen.voice(level)

    async def _refresh(self, always: bool = False) -> None:
        """Send the state to the screen if it changed (or ``always``), and re-arm the
        expiry timer."""
        now = self._now()
        face = self.faces.winner(now)
        scene = self.scenes.winner(now)
        held = (
            self._interaction is not InteractionState.IDLE
            or any(request.content.keep_awake for request in self.scenes.active(now))
            or bool(self._panels)
        )
        woken = held and self.asleep  # something began while the robot slept
        if held != self._held:
            self._held = held
            self._last_activity = now  # the count starts again when it ends
            self._activity.set()
        if woken:  # at once, so that this state shows it; and said, as every wake is
            self.asleep = False
            self.log.info("awake: %s", WakeReason.EVENT.value)
            self.bus.publish(WakeUp(self.name, WakeReason.EVENT))
        state = ScreenState(
            expression=face.content.expression if face else DEFAULT_EXPRESSION,
            tone=face.content.tone if face else None,
            scene=scene.content.name if scene else None,
            scene_data=scene.content.data if scene else {},
            battery=self._battery,
            charging=self._charging,
            battery_low=self._battery_low,
            muted=self._muted,
            panel=self._panel(),
            asleep=self.asleep,
        )
        if always or state != self.state:
            self.state = state
            await self.screen.show(state)
        self._changed.set()
        if woken:
            await self._light()

    def _panel(self) -> Panel | None:
        """The panel asked for last, if any."""
        if not self._panels:
            return None
        return max(self._panels.values(), key=lambda entry: entry[0])[1]

    async def _expire(self) -> None:
        """Refresh when the next request expires, or when the requests change."""
        while True:
            self._changed.clear()
            expiries = (self.faces.next_expiry(), self.scenes.next_expiry())
            deadline = min((d for d in expiries if d is not None), default=None)
            timeout = None if deadline is None else max(0.0, deadline - self._now())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), timeout)
            if deadline is not None and self._now() >= deadline:
                await self._refresh()

    async def _forward_events(self) -> None:
        while True:
            event = await self.screen.event()
            if isinstance(event, Opened):
                if not self.asleep:  # the screen may have turned off before the page held it
                    await self._light()
                continue
            asleep = self.asleep
            self.bus.publish(WakeUp(self.name, WakeReason.TOUCH))
            if asleep:  # the tap that wakes the robot does nothing else
                continue
            if isinstance(event, Touch):
                self.bus.publish(ScreenTouched(event.x, event.y))
                self.capabilities.emit(TOUCHED.name, {"x": event.x, "y": event.y})
            elif isinstance(event, Hold):
                self.bus.publish(ScreenHeld(event.x, event.y))
            elif isinstance(event, Action):
                self.bus.publish(PanelAction(event.panel, event.item, event.value))
            elif isinstance(event, Close):
                self.bus.publish(PanelClosed(event.panel))

    # ---- sleep ----

    async def _on_wake(self, message: WakeUp) -> None:
        self._last_activity = self._now()
        self._activity.set()
        if not self.asleep:
            return
        self.asleep = False
        self.log.info("awake: %s", message.reason.value)
        await self._refresh()
        if message.reason is not WakeReason.TOUCH:  # a touch has lit the screen already
            await self._light()

    async def _fall_asleep(self) -> None:
        """Fall asleep once "Sleep after" minutes pass with nothing going on."""
        while True:
            self._activity.clear()
            if self.asleep or self._held or self.sleep_after == NEVER:
                await self._activity.wait()
                continue
            left = self._last_activity + SECONDS_PER_MINUTE * self.sleep_after - self._now()
            if left > 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._activity.wait(), left)
                continue
            self.asleep = True
            self.log.info("asleep after %g minutes with nothing going on", self.sleep_after)
            await self._refresh()

    async def _light(self) -> None:
        """Turn the screen on, if the system turned it off."""
        try:
            await self.display.wake()
        except OSError as error:
            self.log.warning("cannot light the screen: %s", error)

    async def _set_sleep_after(self, minutes: float) -> None:
        self.sleep_after = minutes  # one of the setting's choices
        self._last_activity = self._now()
        self._activity.set()

    # ---- what the cloud can ask (``capabilities``) ----

    async def _show_expression(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        face = Face(arguments["expression"], arguments.get("tone"))
        seconds = arguments.get("seconds", self.reaction_seconds)
        self.faces.put(
            CLOUD_REACTION, face, priority=ScreenPriority.REACTION, expires=self._expires(seconds)
        )
        return await self._showing_expression()

    async def _set_mood(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        expression = arguments["expression"]
        face = None if expression is None else Face(expression, arguments.get("tone"))
        self.faces.put(CLOUD_MOOD, face, priority=ScreenPriority.AMBIENT)
        return await self._showing_expression()

    async def _show_scene(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        name = arguments["scene"]
        data = arguments.get("data", {})
        scene = None if name is None else Scene(name, data, name in self._keeps_awake)
        self.scenes.put(
            CLOUD_SCENE,
            scene,
            priority=ScreenPriority.REACTION,
            expires=self._expires(arguments.get("seconds")),
        )
        await self._refresh()
        assert self.state is not None
        return {"showing": self.state.scene}

    async def _showing_expression(self) -> Mapping[str, Any]:
        await self._refresh()
        assert self.state is not None
        return {"showing": self.state.expression}

    @staticmethod
    def _now() -> float:
        return asyncio.get_running_loop().time()


def _minutes(minutes: float) -> str:
    return f"{minutes:g} minute" + ("" if minutes == 1 else "s")
