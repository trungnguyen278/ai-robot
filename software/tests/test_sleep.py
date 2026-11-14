# SPDX-License-Identifier: MIT
"""Sleep and wake (docs/decisions/0021-sleep-and-wake.md): when the screen module puts
the robot to sleep, what keeps it awake, and what wakes it."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from ai_robot.adapters.sim.display_power import SimDisplayPower
from ai_robot.adapters.sim.screen import SimScreen
from ai_robot.core.bus import Bus
from ai_robot.core.config import ConfigError
from ai_robot.core.factory import create
from ai_robot.core.module import ModuleContext, RobotInfo
from ai_robot.messages import (
    CapabilityCall,
    CapabilityResult,
    InteractionChanged,
    InteractionState,
    PanelRequest,
    SceneRequest,
    ScreenTouched,
    SettingChanged,
    SettingOffered,
    SettingSection,
    WakeReason,
    WakeUp,
)
from ai_robot.modules.screen import Arbiter, ScreenModule
from ai_robot.ports import DisplayPower, Panel, PanelItem, PanelSection

TIMEOUT = 2.0
# Minutes short enough for a test: 0.002 minutes is 0.12 s.
SHORT = 0.002
LONGER = 0.004
HALF = 60 * SHORT / 2  # seconds: less than a whole count, whatever the clock's grain
SCREEN = {
    "quiet_db": -50.0,
    "loud_db": -10.0,
    "voice_rate": 25.0,
    "reaction_seconds": 3.0,
    "reaction_max_seconds": 30.0,
    "scene_max_seconds": 86400.0,
}


def context(bus: Bus, screen: SimScreen, display: DisplayPower) -> ModuleContext:
    return ModuleContext(
        name="screen",
        bus=bus,
        ports={"screen": screen, "display_power": display},
        robot=RobotInfo(display_name="Robot", kind="robot", model="concept-v0"),
        log=logging.getLogger("test.screen"),
    )


class Robot:
    """A screen module with a simulated screen and display, and what it publishes."""

    def __init__(self, display: DisplayPower | None = None, **sleep: Any) -> None:
        self.bus = Bus()
        self.screen = SimScreen()
        self.display = display or SimDisplayPower()
        ctx = context(self.bus, self.screen, self.display)
        self.results = self.bus.subscribe(CapabilityResult)
        self.offers = self.bus.subscribe(SettingOffered)
        self.touches = self.bus.subscribe(ScreenTouched)
        self.woken = self.bus.subscribe(WakeUp)
        options = {"sleep_after": SHORT, "sleep_choices": [SHORT, LONGER]} | sleep
        self.module = ScreenModule(ctx, **SCREEN, **options)

    @property
    def asleep(self) -> bool:
        return self.screen.state.asleep

    @property
    def wakes(self) -> int:
        assert isinstance(self.display, SimDisplayPower)
        return self.display.wakes

    async def cloud_shows(self, scene: str, data: dict[str, Any]) -> None:
        """Show ``scene`` as the cloud asks for one."""
        arguments = {"scene": scene, "data": data}
        self.bus.publish(CapabilityCall("c1", "screen.show_scene", arguments))
        result = await asyncio.wait_for(self.results.get(), TIMEOUT)
        assert result.error is None


@contextlib.asynccontextmanager
async def running(robot: Robot) -> AsyncIterator[Robot]:
    await robot.module.start()
    task = asyncio.create_task(robot.module.run())
    try:
        yield robot
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await robot.module.stop()


async def until(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + TIMEOUT
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.005)


async def settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


async def asleep_after(robot: Robot) -> float:
    """Wait until the robot sleeps; the seconds that took."""
    start = time.monotonic()
    await until(lambda: robot.asleep)
    return time.monotonic() - start


def panel() -> Panel:
    item = PanelItem("settings.line", "info", "Line", "text")
    return Panel("settings", "Settings", (PanelSection("About", (item,)),))


def test_it_sleeps_after_the_minutes_chosen_with_nothing_going_on() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            assert not robot.asleep
            assert await asleep_after(robot) > HALF
            assert robot.wakes == 0  # the kiosk turns the screen off, not the robot

    asyncio.run(main())


def test_a_wake_up_wakes_it_and_lights_the_screen() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            await asleep_after(robot)
            robot.bus.publish(WakeUp("voice", WakeReason.WAKE_WORD))
            await until(lambda: not robot.asleep)
            assert robot.wakes == 1
            await asleep_after(robot)  # and it counts again from there

    asyncio.run(main())


def test_a_tap_wakes_it_without_lighting_the_screen_and_does_nothing_else() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            await asleep_after(robot)
            robot.screen.tap(0.5, 0.5)
            await until(lambda: not robot.asleep)
            assert robot.wakes == 0  # the touch has lit it
            assert robot.touches.pending == 0  # the tap that wakes it only wakes it
            robot.screen.tap(0.5, 0.5)
            await settle()
            assert robot.touches.pending == 1

    asyncio.run(main())


@pytest.mark.parametrize("kind", ["hold", "act", "close_panel"])
def test_each_thing_done_on_the_screen_wakes_it(kind: str) -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            await asleep_after(robot)
            if kind == "hold":
                robot.screen.hold(0.5, 0.5)
            elif kind == "act":
                robot.screen.act("settings", "voice.volume", 50)
            else:
                robot.screen.close_panel("settings")
            await until(lambda: not robot.asleep)

    asyncio.run(main())


def test_a_wake_up_while_awake_starts_the_count_again() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            for _ in range(4):  # 4 x 0.06 s: twice its time, but never long enough
                await asyncio.sleep(60 * SHORT / 2)
                robot.bus.publish(WakeUp("camera", WakeReason.PRESENCE))
                await settle()
                assert not robot.asleep
            await asleep_after(robot)

    asyncio.run(main())


def test_a_conversation_keeps_it_awake_and_the_count_starts_when_it_ends() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            robot.bus.publish(InteractionChanged(InteractionState.LISTENING))
            await asyncio.sleep(60 * SHORT * 2)
            assert not robot.asleep
            robot.bus.publish(InteractionChanged(InteractionState.IDLE))
            await settle()
            assert await asleep_after(robot) > HALF  # a whole count after it ended

    asyncio.run(main())


def test_a_scene_that_keeps_it_awake_does_and_another_does_not() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            robot.bus.publish(SceneRequest("setup", "setup", {"state": "code"}, keep_awake=True))
            robot.bus.publish(SceneRequest("behavior", "clock"))  # wins, and does not hold
            await asyncio.sleep(60 * SHORT * 2)
            assert not robot.asleep
            robot.bus.publish(SceneRequest("setup", None))
            await settle()
            assert await asleep_after(robot) > HALF

    asyncio.run(main())


def test_a_scene_that_keeps_it_awake_holds_until_it_expires() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            duration = 60 * SHORT * 2
            robot.bus.publish(
                SceneRequest("timers", "timer", duration=duration, priority=10, keep_awake=True)
            )
            await asyncio.sleep(duration * 0.9)
            assert not robot.asleep
            await asleep_after(robot)

    asyncio.run(main())


def test_an_open_panel_keeps_it_awake() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            robot.bus.publish(PanelRequest("settings", panel()))
            await asyncio.sleep(60 * SHORT * 2)
            assert not robot.asleep
            robot.bus.publish(PanelRequest("settings", None))
            await settle()
            assert await asleep_after(robot) > HALF

    asyncio.run(main())


def test_something_that_starts_while_it_sleeps_wakes_it_and_lights_the_screen() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            await asleep_after(robot)
            robot.bus.publish(SceneRequest("setup", "setup", {"state": "code"}, keep_awake=True))
            await until(lambda: not robot.asleep)
            assert robot.wakes == 1
            assert robot.woken.pending == 1  # and said so
            assert await robot.woken.get() == WakeUp("screen", WakeReason.EVENT)
            robot.bus.publish(SceneRequest("behavior", "clock"))  # does not keep it awake
            robot.bus.publish(SceneRequest("setup", None))
            await asleep_after(robot)
            robot.bus.publish(SceneRequest("behavior", "weather"))  # nor wake it
            await settle()
            assert robot.asleep and robot.wakes == 1

    asyncio.run(main())


def test_the_clouds_timer_keeps_it_awake() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            await robot.cloud_shows("timer", {"duration": 60})
            await asyncio.sleep(60 * SHORT * 2)
            assert not robot.asleep
            await robot.cloud_shows("weather", {})  # replaces it, and does not hold
            await asleep_after(robot)

    asyncio.run(main())


def test_a_page_that_opens_lights_the_screen_unless_it_sleeps() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            robot.screen.open_page()
            await until(lambda: robot.wakes == 1)
            await asleep_after(robot)
            robot.screen.open_page()
            await settle()
            assert robot.wakes == 1 and robot.asleep  # it opens asleep

    asyncio.run(main())


class Unplugged(DisplayPower):
    async def wake(self) -> None:
        raise OSError("the wake helper did not answer in time")


def test_a_screen_that_cannot_be_lit_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    async def main() -> None:
        async with running(Robot(display=Unplugged())) as robot:
            await until(lambda: robot.asleep)
            robot.bus.publish(WakeUp("app", WakeReason.APP))
            await until(lambda: not robot.asleep)  # awake all the same
            await settle()

    with caplog.at_level(logging.WARNING, logger="test.screen"):
        asyncio.run(main())
    assert [record.getMessage() for record in caplog.records] == [
        "cannot light the screen: the wake helper did not answer in time"
    ]


def test_sleep_after_is_a_setting_with_never() -> None:
    async def main() -> None:
        robot = Robot(sleep_after=LONGER, sleep_choices=[LONGER, SHORT])
        async with running(robot):
            assert robot.offers.pending == 1
            setting = (await robot.offers.get()).setting
            assert (setting.name, setting.section, setting.title) == (
                "screen.sleep_after",
                SettingSection.DISPLAY,
                "Sleep after",
            )
            assert setting.value == LONGER
            assert list(setting.schema["oneOf"]) == [
                {"const": SHORT, "title": f"{SHORT:g} minutes"},
                {"const": LONGER, "title": f"{LONGER:g} minutes"},
                {"const": 0, "title": "Never"},
            ]
            robot.bus.publish(SettingChanged("screen.sleep_after", 0))
            await settle()
            await asyncio.sleep(60 * LONGER * 1.5)
            assert not robot.asleep  # never
            robot.bus.publish(SettingChanged("screen.sleep_after", SHORT))
            await settle()
            # The count starts when the choice changes: a shorter one never ends it at once.
            assert await asleep_after(robot) >= HALF
            robot.bus.publish(SettingChanged("screen.sleep_after", 3))  # not a choice
            await settle()
            assert robot.module.sleep_after == SHORT

    asyncio.run(main())


def test_the_choices_are_named_in_minutes() -> None:
    async def main() -> None:
        robot = Robot(sleep_after=5, sleep_choices=[30, 1, 5])
        async with running(robot):
            setting = (await robot.offers.get()).setting
            titles = [choice["title"] for choice in setting.schema["oneOf"]]
            assert titles == ["1 minute", "5 minutes", "30 minutes", "Never"]

    asyncio.run(main())


def test_a_value_kept_from_before_applies_before_it_counts() -> None:
    async def main() -> None:
        robot = Robot(sleep_after=SHORT, sleep_choices=[SHORT, LONGER])
        robot.bus.publish(SettingChanged("screen.sleep_after", 0))  # as the settings keep it
        async with running(robot):
            await asyncio.sleep(60 * SHORT * 2)
            assert not robot.asleep

    asyncio.run(main())


def test_sleep_settings_are_checked() -> None:
    ctx = context(Bus(), SimScreen(), SimDisplayPower())
    good = {**SCREEN, "sleep_after": 5, "sleep_choices": [1, 2, 5, 10, 30]}
    for change, message in [
        ({"sleep_choices": []}, "sleep_choices must list distinct numbers of minutes"),
        ({"sleep_choices": [1, 1, 5]}, "sleep_choices must list distinct numbers of minutes"),
        ({"sleep_choices": [0, 5]}, "sleep_choices must be a positive number"),
        ({"sleep_after": 3}, "sleep_after must be one of sleep_choices, or 0 for never"),
    ]:
        with pytest.raises(ConfigError, match=message):
            create(ScreenModule, "modules.screen", ctx, **{**good, **change})
    create(ScreenModule, "modules.screen", ctx, **{**good, "sleep_after": 0})  # never


def test_arbiter_lists_the_requests_that_have_not_expired() -> None:
    arbiter: Arbiter[str] = Arbiter()
    arbiter.put("a", "happy", priority=50, expires=1.0)
    arbiter.put("b", "sad", priority=10)
    assert [r.content for r in arbiter.active(0.5)] == ["happy", "sad"]
    assert [r.content for r in arbiter.active(1.0)] == ["sad"]
    assert arbiter.winner(1.0).content == "sad"  # type: ignore[union-attr]
