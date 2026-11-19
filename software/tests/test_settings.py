# SPDX-License-Identifier: MIT
"""The settings: modules offer them, the panel shows them, and what the owner chooses
is kept and applied (docs/decisions/0016-settings.md)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from functools import partial
from typing import Any

import pytest
import waiting

from ai_robot.adapters.sim.settings import SimSettingsStore
from ai_robot.core.bus import Bus
from ai_robot.core.module import Module, ModuleContext, RobotInfo
from ai_robot.core.settings import SettingsHost
from ai_robot.messages import (
    CapabilityCall,
    CapabilityResult,
    PanelAction,
    PanelClosed,
    PanelRequest,
    ResetStage,
    Resetting,
    ScreenHeld,
    Setting,
    SettingChanged,
    SettingKind,
    SettingOffered,
    SettingPressed,
    SettingRequested,
    SettingSection,
    SettingsOpened,
)
from ai_robot.modules.settings import PANEL, SettingsModule, item_of
from ai_robot.ports import Panel, SettingsStore

TIMEOUT = 2.0
until = partial(waiting.until, timeout=TIMEOUT)
COLOURS = {"oneOf": [{"const": "red", "title": "Red"}, {"const": "green", "title": "Green"}]}


def context(name: str, bus: Bus, ports: dict[str, Any] | None = None) -> ModuleContext:
    return ModuleContext(
        name=name,
        bus=bus,
        ports=ports or {},
        robot=RobotInfo(display_name="Robot", kind="robot", model="concept-v0"),
        log=logging.getLogger(f"test.{name}"),
    )


class Lamp(Module):
    """A module with one setting of each kind."""

    def __init__(self, ctx: ModuleContext, refuse: bool = False) -> None:
        super().__init__(ctx)
        self.on = True
        self.brightness = 50
        self.colour = "red"
        self.blinks = 0
        self.refuse = refuse
        self.settings = SettingsHost(self)
        sound = SettingSection.SOUND  # any section will do
        self.settings.add(
            Setting("lamp.on", SettingKind.VALUE, sound, "On", True, schema={"type": "boolean"}),
            self._set_on,
        )
        self.settings.add(
            Setting(
                "lamp.brightness",
                SettingKind.VALUE,
                sound,
                "Brightness",
                50,
                schema={"type": "integer", "minimum": 0, "maximum": 100, "multipleOf": 10},
                unit="%",
            ),
            self._set_brightness,
        )
        self.settings.add(
            Setting("lamp.colour", SettingKind.VALUE, sound, "Colour", "red", schema=COLOURS),
            self._set_colour,
        )
        self.settings.add(
            Setting("lamp.blink", SettingKind.ACTION, SettingSection.NETWORK, "Blink"), self._blink
        )
        self.settings.add(
            Setting("lamp.leave", SettingKind.ACTION, SettingSection.NETWORK, "Leave", closes=True),
            self._blink,
        )
        self.settings.add(
            Setting("lamp.state", SettingKind.INFO, SettingSection.ABOUT, "State", "lit")
        )

    async def start(self) -> None:
        self.settings.offer()

    async def run(self) -> None:
        await self.settings.apply_pending()
        await self.settings.serve()

    async def stop(self) -> None:
        self.settings.close()

    async def _set_on(self, on: bool) -> None:
        self.on = on

    async def _set_brightness(self, value: int) -> None:
        if self.refuse:
            raise ValueError("the bulb is too weak")
        self.brightness = value

    async def _set_colour(self, colour: str) -> None:
        self.colour = colour

    async def _blink(self, _: None) -> None:
        self.blinks += 1


class Robot:
    def __init__(self, store: SettingsStore | None = None, refuse: bool = False) -> None:
        self.bus = Bus()
        self.store = store or SimSettingsStore()
        self.settings = SettingsModule(context("settings", self.bus, {"settings": self.store}))
        self.lamp = Lamp(context("lamp", self.bus), refuse=refuse)
        self.panels = self.bus.subscribe(PanelRequest)
        self.opened = self.bus.subscribe(SettingsOpened)

    async def panel(self) -> Panel | None:
        """The next panel the settings ask the screen for."""
        return (await asyncio.wait_for(self.panels.get(), TIMEOUT)).panel

    async def panel_where(self, check: Any) -> Panel:
        """The next panel for which ``check`` holds."""
        async with asyncio.timeout(TIMEOUT):
            while True:
                panel = await self.panel()
                if panel is not None and check(panel):
                    return panel

    def act(self, item: str, value: Any = None) -> None:
        self.bus.publish(PanelAction(PANEL, item, value))


@contextlib.asynccontextmanager
async def running(robot: Robot) -> AsyncIterator[Robot]:
    modules = (robot.settings, robot.lamp)  # in the configured order: settings first
    for module in modules:
        await module.start()
    tasks = [asyncio.create_task(module.run()) for module in modules]
    try:
        await asyncio.sleep(0)
        yield robot
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for module in reversed(modules):
            await module.stop()


def value(panel: Panel, item: str) -> Any:
    found = panel.item(item)
    assert found is not None
    return found.value


def test_a_held_press_opens_the_panel_with_every_setting() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            robot.bus.publish(ScreenHeld(0.5, 0.5))
            panel = await robot.panel()
            assert panel is not None and panel.name == PANEL and panel.title == "Settings"
            assert [section.title for section in panel.sections] == [
                SettingSection.NETWORK.value,  # the sections' order, whoever offered first
                SettingSection.SOUND.value,
                SettingSection.ABOUT.value,
            ]
            kinds = {item.id: item.kind for s in panel.sections for item in s.items}
            assert kinds == {
                "lamp.blink": "button",
                "lamp.leave": "button",
                "lamp.on": "toggle",
                "lamp.brightness": "slider",
                "lamp.colour": "choice",
                "settings.name": "info",
                "settings.software": "info",
                "lamp.state": "info",
            }
            slider = panel.item("lamp.brightness")
            assert slider is not None
            assert (slider.minimum, slider.maximum, slider.step, slider.unit) == (0, 100, 10, "%")
            colour = panel.item("lamp.colour")
            assert colour is not None and colour.choices == (("red", "Red"), ("green", "Green"))
            assert value(panel, "settings.name") == "Robot"
            await asyncio.wait_for(robot.opened.get(), TIMEOUT)  # modules refresh their lines

    asyncio.run(main())


def test_a_change_is_kept_and_applied_by_its_module() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            robot.bus.publish(ScreenHeld(0.5, 0.5))
            await robot.panel()
            robot.act("lamp.brightness", 70)
            panel = await robot.panel_where(lambda p: value(p, "lamp.brightness") == 70)
            await until(lambda: robot.lamp.brightness == 70)
            assert robot.store.values == {"lamp.brightness": 70}
            robot.act("lamp.colour", "green")
            robot.act("lamp.on", False)
            await until(lambda: (robot.lamp.colour, robot.lamp.on) == ("green", False))
            assert robot.store.values == {
                "lamp.brightness": 70,
                "lamp.colour": "green",
                "lamp.on": False,
            }
            assert panel.item("lamp.state") is not None

    asyncio.run(main())


def test_a_choice_made_elsewhere_is_kept_and_applied_as_one_made_on_the_panel() -> None:
    """Such as the languages the app gives at set-up: the panel may be closed."""

    async def main() -> None:
        async with running(Robot()) as robot:
            robot.bus.publish(SettingRequested("lamp.colour", "green"))
            await until(lambda: robot.lamp.colour == "green")
            assert robot.store.values == {"lamp.colour": "green"}
            assert robot.panels.pending == 0  # nothing to show: the panel is closed
            robot.bus.publish(SettingRequested("lamp.colour", "blue"))  # not a choice
            robot.bus.publish(SettingRequested("lamp.state", "dim"))  # a line, not a value
            robot.bus.publish(SettingRequested("lamp.on", False))
            await until(lambda: robot.lamp.on is False)
            assert robot.store.values == {"lamp.colour": "green", "lamp.on": False}

    asyncio.run(main())


def test_values_that_break_the_schema_are_neither_kept_nor_applied() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            robot.bus.publish(ScreenHeld(0.5, 0.5))
            await robot.panel()
            robot.act("lamp.brightness", 65)  # not a step of 10
            panel = await robot.panel()  # sent again, as it is
            assert panel is not None and value(panel, "lamp.brightness") == 50
            robot.act("lamp.colour", "blue")
            await robot.panel()
            assert robot.store.values == {} and robot.lamp.colour == "red"

    asyncio.run(main())


def test_a_change_shows_before_its_module_takes_it() -> None:
    # Whatever the module does on the change (the voice says the microphones are off),
    # the screen already has the panel with the new value: no state shows the old one.
    async def main() -> None:
        async with running(Robot()) as robot:
            changes = robot.bus.subscribe(SettingChanged)
            robot.bus.publish(ScreenHeld(0.5, 0.5))
            await robot.panel()
            robot.act("lamp.on", False)
            await asyncio.wait_for(changes.get(), TIMEOUT)
            assert robot.panels.pending >= 1  # there before the module hears of the change
            panel = await robot.panel()
            assert panel is not None and value(panel, "lamp.on") is False

    asyncio.run(main())


def test_a_value_its_module_cannot_apply_goes_back() -> None:
    async def main() -> None:
        async with running(Robot(refuse=True)) as robot:
            robot.bus.publish(ScreenHeld(0.5, 0.5))
            await robot.panel()
            robot.act("lamp.brightness", 70)
            shown = await robot.panel()  # at once
            assert shown is not None and value(shown, "lamp.brightness") == 70
            panel = await robot.panel()  # the lamp could not take it
            assert panel is not None and value(panel, "lamp.brightness") == 50
            assert robot.lamp.brightness == 50

    asyncio.run(main())


def test_actions_run_and_one_that_leaves_closes_the_panel() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            robot.bus.publish(ScreenHeld(0.5, 0.5))
            await robot.panel()
            robot.act("lamp.blink")
            robot.act("lamp.state")  # a line to read: nothing to do
            robot.act("lamp.leave")
            assert await robot.panel() is None  # closed
            assert robot.lamp.blinks == 2 and robot.store.values == {}
            robot.act("lamp.blink")  # the panel is closed: ignored
            await asyncio.sleep(0.05)
            assert robot.lamp.blinks == 2

    asyncio.run(main())


def test_a_factory_reset_closes_the_panel_and_keeps_it_closed() -> None:
    """The owner held the face again as the reset began: its news closes the panel."""

    async def main() -> None:
        async with running(Robot()) as robot:
            robot.bus.publish(ScreenHeld(0.5, 0.5))
            await robot.panel()
            robot.bus.publish(Resetting(ResetStage.LEAVING))
            assert await robot.panel() is None
            robot.bus.publish(ScreenHeld(0.5, 0.5))
            await asyncio.sleep(0.05)
            assert robot.panels.pending == 0

    asyncio.run(main())


def test_the_owner_closes_the_panel() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            robot.bus.publish(ScreenHeld(0.5, 0.5))
            await robot.panel()
            robot.bus.publish(PanelClosed("another panel"))
            robot.bus.publish(PanelClosed(PANEL))
            assert await robot.panel() is None

    asyncio.run(main())


def test_kept_values_apply_before_the_modules_run() -> None:
    async def main() -> None:
        store = SimSettingsStore()
        store.values = {"lamp.on": False, "lamp.brightness": 30, "elsewhere.thing": 1}
        robot = Robot(store)
        await robot.settings.start()  # settings starts first: it sends what it kept
        await robot.lamp.start()
        await robot.lamp.settings.apply_pending()  # as the lamp does first in run()
        assert (robot.lamp.on, robot.lamp.brightness) == (False, 30)
        await robot.lamp.stop()
        await robot.settings.stop()

    asyncio.run(main())


def test_nothing_but_a_press_does_an_action() -> None:
    """Not a value kept for it, as a settings file may hold any name, nor one sent."""

    async def main() -> None:
        store = SimSettingsStore()
        store.values = {"lamp.blink": None, "lamp.leave": None}
        async with running(Robot(store)) as robot:
            robot.bus.publish(SettingChanged("lamp.blink"))
            robot.bus.publish(SettingChanged("lamp.brightness", 70))  # after it, in order
            await waiting.until(lambda: robot.lamp.brightness == 70, TIMEOUT)
            assert robot.lamp.blinks == 0
            robot.bus.publish(SettingPressed("lamp.brightness"))  # a value: no press
            robot.bus.publish(ScreenHeld(0.5, 0.5))
            await robot.panel()
            robot.act("lamp.blink")
            await waiting.until(lambda: robot.lamp.blinks == 1, TIMEOUT)
            assert robot.lamp.brightness == 70  # the press before it changed nothing

    asyncio.run(main())


def test_the_cloud_can_open_the_settings() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            results = robot.bus.subscribe(CapabilityResult)
            robot.bus.publish(CapabilityCall("c1", "settings.open", {}))
            result = await asyncio.wait_for(results.get(), TIMEOUT)
            assert (result.call, dict(result.data), result.error) == ("c1", {"open": True}, None)
            assert (await robot.panel()) is not None

    asyncio.run(main())


class Unsaved(SimSettingsStore):
    async def save(self, values: Any) -> None:
        raise OSError("no space left on device")


def test_a_change_that_cannot_be_kept_still_applies(caplog: pytest.LogCaptureFixture) -> None:
    async def main() -> None:
        async with running(Robot(Unsaved())) as robot:
            robot.bus.publish(ScreenHeld(0.5, 0.5))
            await robot.panel()
            robot.act("lamp.brightness", 90)
            await robot.panel_where(lambda p: value(p, "lamp.brightness") == 90)

    with caplog.at_level(logging.ERROR):
        asyncio.run(main())
    assert "cannot keep the robot's settings: no space left on device" in caplog.text


def test_settings_that_are_not_their_modules_or_not_drawable_are_refused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            schema = {"type": "string"}  # the panel has no text field
            text = Setting(
                "lamp.label", SettingKind.VALUE, SettingSection.SOUND, "Label", "x", schema=schema
            )
            robot.bus.publish(SettingOffered("lamp", text))
            stolen = Setting("voice.volume", SettingKind.INFO, SettingSection.SOUND, "Volume", "1")
            robot.bus.publish(SettingOffered("lamp", stolen))
            robot.bus.publish(ScreenHeld(0.5, 0.5))
            panel = await robot.panel()
            assert panel is not None
            assert panel.item("lamp.label") is None and panel.item("voice.volume") is None

    with caplog.at_level(logging.ERROR):
        asyncio.run(main())
    assert "the panel cannot show its schema" in caplog.text
    assert "lamp offers setting voice.volume: not its own" in caplog.text


@pytest.mark.parametrize(
    ("fields", "problem"),
    [
        ({"name": "Volume"}, "owner.name"),
        ({"title": ""}, "needs a title"),
        ({"kind": SettingKind.VALUE}, "has a schema"),
        ({"kind": SettingKind.ACTION, "value": 1}, "no value"),
        ({"value": 3}, "its text"),
        ({"value": float("nan")}, "finite"),
        ({"closes": True}, "only an action"),
    ],
)
def test_settings_are_checked(fields: dict[str, Any], problem: str) -> None:
    good: dict[str, Any] = {
        "name": "lamp.state",
        "kind": SettingKind.INFO,
        "section": SettingSection.ABOUT,
        "title": "State",
        "value": "lit",
    }
    with pytest.raises(ValueError, match=problem):
        Setting(**{**good, **fields})


def test_a_module_offers_only_its_own_settings_with_valid_values() -> None:
    lamp = Lamp(context("lamp", Bus()))
    with pytest.raises(ValueError, match=r"must start with lamp\."):
        lamp.settings.add(Setting("other.x", SettingKind.INFO, SettingSection.ABOUT, "X", "x"))
    schema = {"type": "integer", "minimum": 0, "maximum": 10}
    with pytest.raises(ValueError, match="breaks its schema"):
        lamp.settings.add(
            Setting("lamp.x", SettingKind.VALUE, SettingSection.ABOUT, "X", 11, schema=schema),
            lamp._set_on,
        )
    with pytest.raises(ValueError, match="need a handler"):
        lamp.settings.add(Setting("lamp.y", SettingKind.ACTION, SettingSection.ABOUT, "Y"))


def test_a_number_without_steps_moves_by_hundredths() -> None:
    schema = {"type": "number", "minimum": 0, "maximum": 2}
    item = item_of(Setting("a.b", SettingKind.VALUE, SettingSection.SOUND, "B", 1.0, schema=schema))
    assert (item.kind, item.step) == ("slider", 0.02)
