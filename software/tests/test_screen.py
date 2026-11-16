# SPDX-License-Identifier: MIT
"""The screen module: requests, scenes, the conversation, the status row, voice, gaze, taps."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path

import pytest

from ai_robot.adapters.sim.display_power import SimDisplayPower
from ai_robot.adapters.sim.screen import SimScreen
from ai_robot.core.bus import Bus
from ai_robot.core.config import ConfigError
from ai_robot.core.factory import create
from ai_robot.core.module import ModuleContext, RobotInfo
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
    SoundLevel,
)
from ai_robot.modules.screen import (
    DEFAULT_EXPRESSION,
    INTERACTION_EXPRESSIONS,
    Arbiter,
    ScreenModule,
)
from ai_robot.ports import BatteryStatus, Panel, PanelItem, PanelSection
from ai_robot.ports.screen import PANEL_KINDS

VOCABULARY = Path(__file__).resolve().parents[2] / "protocol" / "screen" / "vocabulary.json"

# The screen module's settings, as shipped in config/base.toml.
SCREEN = {
    "voice_rate": 25.0,
    "reaction_seconds": 3.0,
    "reaction_max_seconds": 30.0,
    "scene_max_seconds": 86400.0,
    "sleep_after": 5,
    "sleep_choices": [1, 2, 5, 10, 30],
}


def context(bus: Bus, screen: SimScreen) -> ModuleContext:
    return ModuleContext(
        name="screen",
        bus=bus,
        ports={"screen": screen, "display_power": SimDisplayPower()},
        robot=RobotInfo(display_name="Robot", kind="robot", model="concept-v0"),
        log=logging.getLogger("test"),
    )


def make() -> tuple[Bus, SimScreen, ScreenModule]:
    bus = Bus()
    screen = SimScreen()
    module = ScreenModule(context(bus, screen), quiet_db=-50.0, loud_db=-10.0, **SCREEN)
    return bus, screen, module


@contextlib.asynccontextmanager
async def running(module: ScreenModule) -> AsyncIterator[None]:
    await module.start()
    task = asyncio.create_task(module.run())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await module.stop()


async def settle() -> None:
    """Let the module handle what was published."""
    for _ in range(10):
        await asyncio.sleep(0)


def test_arbiter_picks_the_highest_priority_then_the_newest() -> None:
    arbiter: Arbiter[str] = Arbiter()
    assert arbiter.winner(0) is None
    arbiter.put("a", "happy", priority=10)
    arbiter.put("b", "sad", priority=50)
    arbiter.put("c", "angry", priority=10)
    assert arbiter.winner(0).content == "sad"  # type: ignore[union-attr]
    arbiter.put("b", None, priority=50)  # withdrawn
    assert arbiter.winner(0).content == "angry"  # type: ignore[union-attr]
    arbiter.put("a", "love", priority=10)  # a source replaces its own request
    assert arbiter.winner(0).content == "love"  # type: ignore[union-attr]


def test_arbiter_drops_expired_requests() -> None:
    arbiter: Arbiter[str] = Arbiter()
    arbiter.put("a", "happy", priority=50, expires=1.0)
    arbiter.put("b", "sad", priority=10, expires=3.0)
    assert arbiter.next_expiry() == 1.0
    assert arbiter.winner(0.5).content == "happy"  # type: ignore[union-attr]
    assert arbiter.winner(1.0).content == "sad"  # type: ignore[union-attr]
    assert arbiter.next_expiry() == 3.0
    assert arbiter.winner(3.0) is None
    assert arbiter.next_expiry() is None


def test_the_first_state_is_shown_at_start() -> None:
    async def main() -> None:
        _, screen, module = make()
        async with running(module):
            assert screen.updates == 1
            assert (screen.state.expression, screen.state.scene) == (DEFAULT_EXPRESSION, None)

    asyncio.run(main())


def test_the_conversation_drives_the_face() -> None:
    async def main() -> None:
        bus, screen, module = make()
        async with running(module):
            for state, expression in [
                (InteractionState.TRIGGERED, "listening"),
                (InteractionState.ATTENTIVE, "listening"),
                (InteractionState.LISTENING, "listening"),
                (InteractionState.PROCESSING, "thinking"),
                (InteractionState.SPEAKING, "speaking"),
                (InteractionState.IDLE, DEFAULT_EXPRESSION),
            ]:
                bus.publish(InteractionChanged(state))
                await settle()
                assert screen.state.expression == expression, state

    asyncio.run(main())


def test_requests_outrank_the_conversation_and_expire() -> None:
    async def main() -> None:
        bus, screen, module = make()
        async with running(module):
            bus.publish(InteractionChanged(InteractionState.LISTENING))
            bus.publish(ExpressionRequest("behavior", "sad", priority=ScreenPriority.AMBIENT))
            await settle()
            assert screen.state.expression == "listening"  # the ambient mood waits

            bus.publish(ExpressionRequest("vision", "surprised", duration=0.05, tone="white"))
            await settle()
            assert (screen.state.expression, screen.state.tone) == ("surprised", "white")
            await asyncio.sleep(0.1)
            await settle()
            assert (screen.state.expression, screen.state.tone) == ("listening", None)

            bus.publish(InteractionChanged(InteractionState.IDLE))
            await settle()
            assert screen.state.expression == "sad"
            bus.publish(ExpressionRequest("behavior", None))
            await settle()
            assert screen.state.expression == DEFAULT_EXPRESSION

    asyncio.run(main())


def test_scenes_come_and_go_while_the_face_keeps_its_expression() -> None:
    async def main() -> None:
        bus, screen, module = make()
        async with running(module):
            bus.publish(InteractionChanged(InteractionState.LISTENING))
            bus.publish(SceneRequest("behavior", "weather", {"condition": "rain"}, duration=0.05))
            await settle()
            assert screen.state.scene == "weather"
            assert dict(screen.state.scene_data) == {"condition": "rain"}
            assert screen.state.expression == "listening"
            await asyncio.sleep(0.1)
            await settle()
            assert screen.state.scene is None

            bus.publish(SceneRequest("behavior", "clock"))
            bus.publish(SceneRequest("timers", "timer", {"duration": 60}, priority=80))
            await settle()
            assert screen.state.scene == "timer"
            bus.publish(SceneRequest("timers", None))
            await settle()
            assert screen.state.scene == "clock"

    asyncio.run(main())


def test_only_changes_reach_the_screen() -> None:
    async def main() -> None:
        bus, screen, module = make()
        async with running(module):
            shown = screen.updates
            for _ in range(3):
                bus.publish(BatteryReport(BatteryStatus(voltage=7.4, percent=63.0, charging=True)))
            bus.publish(SceneRequest("behavior", "weather", {"days": [18, 19]}))
            bus.publish(SceneRequest("behavior", "weather", {"days": [18, 19]}))  # the same
            await settle()
            assert (screen.state.battery, screen.state.charging) == (63.0, True)
            assert screen.updates == shown + 2

    asyncio.run(main())


def test_the_battery_is_shown_low_when_the_system_says_so() -> None:
    async def main() -> None:
        bus, screen, module = make()
        async with running(module):
            empty = BatteryStatus(voltage=6.6, percent=9.0, charging=False)
            bus.publish(BatteryReport(empty, low=True))
            await settle()
            assert screen.state.battery_low is True
            bus.publish(BatteryReport(empty))  # no judgement: unknown
            await settle()
            assert screen.state.battery_low is None

    asyncio.run(main())


def test_the_voice_follows_the_microphone_only_while_listening() -> None:
    async def main() -> None:
        bus, screen, module = make()
        async with running(module):
            bus.publish(SoundLevel(rms=3277.0))  # -20 dBFS
            await settle()
            assert screen.level == 0.0  # not listening
            bus.publish(InteractionChanged(InteractionState.LISTENING))
            await settle()
            bus.publish(SoundLevel(rms=3277.0))
            await settle()
            assert screen.level == pytest.approx(0.75, abs=0.01)
            bus.publish(InteractionChanged(InteractionState.PROCESSING))
            await settle()
            assert screen.level == 0.0
            bus.publish(InteractionChanged(InteractionState.ATTENTIVE))  # between turns
            async with asyncio.timeout(2.0):  # the next level the voice's rate lets through
                while screen.level == 0.0:
                    bus.publish(SoundLevel(rms=3277.0))
                    await asyncio.sleep(0.01)
            assert screen.level == pytest.approx(0.75, abs=0.01)

    asyncio.run(main())


def test_loudness_scale() -> None:
    _, _, module = make()
    assert module.loudness(0) == 0.0
    assert module.loudness(32768 * 10 ** (-50 / 20)) == pytest.approx(0.0)
    assert module.loudness(32768 * 10 ** (-30 / 20)) == pytest.approx(0.5)
    assert module.loudness(32768.0) == 1.0


def test_gaze_and_taps() -> None:
    async def main() -> None:
        bus, screen, module = make()
        taps = bus.subscribe(ScreenTouched)
        async with running(module):
            bus.publish(LookAt((0.5, -0.2)))
            await settle()
            assert screen.gaze == (0.5, -0.2)
            bus.publish(LookAt(None))
            await settle()
            assert screen.gaze is None
            screen.tap(0.25, 0.75)
            await settle()
            assert taps.pending == 1
            assert await taps.get() == ScreenTouched(0.25, 0.75)

    asyncio.run(main())


def test_gaze_is_checked_and_kept_on_the_screen() -> None:
    assert LookAt((2, -1.5)).target == (1.0, -1.0)
    with pytest.raises(ValueError, match="x must be a finite number"):
        LookAt((math.nan, 0.0))


def test_scene_data_is_a_deep_read_only_copy() -> None:
    days = [18, {"high": 19}]
    request = SceneRequest("behavior", "weather", {"days": days})
    days.append(20)  # the publisher's own list
    assert request.data["days"] == (18, {"high": 19})
    with pytest.raises(TypeError):
        request.data["condition"] = "fog"  # type: ignore[index]
    with pytest.raises(TypeError):
        request.data["days"][1]["high"] = 0  # type: ignore[index]


@pytest.mark.parametrize(
    "data",
    [
        {"temperature": math.nan},
        {"temperature": math.inf},
        {"hourly": {(9, 0): 18}},
        {"day": date(2026, 10, 17)},
    ],
)
def test_scene_data_must_convert_to_json(data: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="scene data must convert to JSON"):
        SceneRequest("behavior", "weather", data)


def test_settings_are_checked() -> None:
    ctx = context(Bus(), SimScreen())
    good = {"quiet_db": -50.0, "loud_db": -10.0, **SCREEN}
    for change, message in [
        ({"quiet_db": -10.0, "loud_db": -50.0}, "loud_db must be above quiet_db"),
        ({"voice_rate": 0}, "voice_rate must be a positive number"),
        ({"reaction_seconds": 40.0}, "reaction_seconds must not exceed reaction_max_seconds"),
        ({"scene_max_seconds": -1}, "scene_max_seconds must be a positive number"),
    ]:
        with pytest.raises(ConfigError, match=message):
            create(ScreenModule, "modules.screen", ctx, **{**good, **change})
    for duration in (0, -1, math.inf):
        with pytest.raises(ValueError, match="duration must be a positive number"):
            ExpressionRequest("behavior", "happy", duration=duration)
        with pytest.raises(ValueError, match="duration must be a positive number"):
            SceneRequest("behavior", "clock", duration=duration)


def test_the_expressions_used_are_in_the_protocol() -> None:
    vocabulary = json.loads(VOCABULARY.read_text(encoding="utf-8"))
    expressions = {DEFAULT_EXPRESSION} | {e for e in INTERACTION_EXPRESSIONS.values() if e}
    assert expressions <= set(vocabulary["expressions"])


def test_the_kinds_of_panel_items_are_the_protocols() -> None:
    vocabulary = json.loads(VOCABULARY.read_text(encoding="utf-8"))
    assert tuple(vocabulary["panel_items"]) == PANEL_KINDS


def panel(name: str) -> Panel:
    item = PanelItem(f"{name}.line", "info", "Line", "text")
    return Panel(name, name.title(), (PanelSection("About", (item,)),))


def test_the_latest_panel_asked_for_shows_over_the_face() -> None:
    async def main() -> None:
        bus, screen, module = make()
        async with running(module):
            bus.publish(PanelRequest("settings", panel("settings")))
            await settle()
            assert screen.state.panel == panel("settings")
            bus.publish(PanelRequest("help", panel("help")))
            await settle()
            assert screen.state.panel == panel("help")
            bus.publish(PanelRequest("help", None))
            await settle()
            assert screen.state.panel == panel("settings")  # the one asked for before
            bus.publish(PanelRequest("settings", None))
            await settle()
            assert screen.state.panel is None

    asyncio.run(main())


def test_a_panel_asked_for_again_is_sent_again() -> None:
    # The renderer shows a change at once; the same panel sent again puts back one
    # the robot refused.
    async def main() -> None:
        bus, screen, module = make()
        async with running(module):
            bus.publish(PanelRequest("settings", panel("settings")))
            await settle()
            sent = screen.updates
            bus.publish(PanelRequest("settings", panel("settings")))
            await settle()
            assert screen.updates == sent + 1
            assert screen.state.panel == panel("settings")

    asyncio.run(main())


def test_the_status_row_says_when_the_microphones_are_off() -> None:
    async def main() -> None:
        bus, screen, module = make()
        async with running(module):
            assert screen.state.muted is None  # unknown until voice says
            bus.publish(MicrophonesChanged(False))
            await settle()
            assert screen.state.muted is True
            bus.publish(MicrophonesChanged(True))
            await settle()
            assert screen.state.muted is False

    asyncio.run(main())


def test_holds_and_what_is_done_on_a_panel_are_published() -> None:
    async def main() -> None:
        bus, screen, module = make()
        holds = bus.subscribe(ScreenHeld)
        actions = bus.subscribe(PanelAction)
        closings = bus.subscribe(PanelClosed)
        async with running(module):
            screen.hold(0.5, 0.25)
            screen.act("settings", "voice.volume", 60)
            screen.act("settings", "setup.start")
            screen.close_panel("settings")
            await settle()
            assert await holds.get() == ScreenHeld(0.5, 0.25)
            assert await actions.get() == PanelAction("settings", "voice.volume", 60)
            assert await actions.get() == PanelAction("settings", "setup.start")
            assert await closings.get() == PanelClosed("settings")

    asyncio.run(main())
