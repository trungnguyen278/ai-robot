# SPDX-License-Identifier: MIT
"""Settings: what the owner sees and changes on the robot's touch screen
(docs/decisions/0016-settings.md)."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, TypeVar

from ai_robot import software_version
from ai_robot.core.bus import Message, Subscription
from ai_robot.core.capabilities import CapabilityHost
from ai_robot.core.data import Schema
from ai_robot.core.module import Module, ModuleContext
from ai_robot.core.settings import SettingsHost
from ai_robot.messages import (
    PanelAction,
    PanelClosed,
    PanelRequest,
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
from ai_robot.ports.cloud import Capability, SafetyClass
from ai_robot.ports.screen import Panel, PanelItem, PanelSection
from ai_robot.ports.settings import SettingsStore

M = TypeVar("M", bound=Message)

PANEL = "settings"  # the panel's name, which the screen's actions carry
TITLE = "Settings"

OPEN = Capability(
    name="settings.open",
    title="Open the settings",
    description=(
        "Show the robot's settings on its screen, for the person in front of it to "
        "change: Wi-Fi and app, sound, what the cloud may do."
    ),
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    output_schema={
        "type": "object",
        "properties": {"open": {"type": "boolean"}},
        "required": ["open"],
        "additionalProperties": False,
    },
    safety=SafetyClass.INFORMATIONAL,
)


def item_of(setting: Setting) -> PanelItem:
    """How the panel shows ``setting``; ``ValueError`` for a schema it cannot show."""
    common = {"help": setting.help}
    asked = {
        "confirm": setting.confirm,
        "confirm_lines": setting.confirm_lines,
        "confirm_label": setting.confirm_label,
    }
    if setting.kind is SettingKind.ACTION:
        return PanelItem(setting.name, "button", setting.title, **asked, **common)
    if setting.kind is SettingKind.INFO:
        return PanelItem(setting.name, "info", setting.title, setting.value, **common)
    schema = setting.schema or {}
    kind = schema.get("type")
    if kind == "boolean":
        return PanelItem(setting.name, "toggle", setting.title, setting.value, **asked, **common)
    if kind in ("number", "integer") and "minimum" in schema and "maximum" in schema:
        low, high = schema["minimum"], schema["maximum"]
        step = schema.get("multipleOf", 1 if kind == "integer" else (high - low) / 100)
        return PanelItem(
            setting.name,
            "slider",
            setting.title,
            setting.value,
            minimum=low,
            maximum=high,
            step=step,
            unit=setting.unit,
            **common,
        )
    options = schema.get("oneOf")
    if options and all(isinstance(option, Mapping) and "const" in option for option in options):
        choices = tuple((o["const"], o.get("title", str(o["const"]))) for o in options)
        return PanelItem(
            setting.name, "choice", setting.title, setting.value, choices=choices, **common
        )
    raise ValueError(f"setting {setting.name}: the panel cannot show its schema")


class SettingsModule(Module):
    """Gathers the settings the modules offer, keeps what the owner chose, and shows
    them as a panel on the screen (docs/decisions/0016-settings.md).

    - **Offers.** Modules offer their settings (``SettingOffered``); a new offer of the
      same name tells its new value. Each module applies its own.
    - **Kept values.** A change made on the panel, or elsewhere such as in the app at
      set-up (``SettingRequested``), is checked against the setting's schema, kept in
      the ``settings`` port, and sent to its module
      (``SettingChanged``). At start, the values kept are sent again, before the
      modules run, so that they apply them before anything relies on them.
    - **The panel.** A press held on the face (``ScreenHeld``), or the cloud's
      ``settings.open``, opens it; the owner closes it, or the screen does after a
      while (``PanelClosed``). An action pressed goes to its module (``SettingPressed``):
      never from what was kept. An action that leaves, such as opening set-up, closes it.
      A factory reset (``Resetting``) closes it, and keeps it closed.
    """

    owns = ("settings",)

    def __init__(self, ctx: ModuleContext) -> None:
        super().__init__(ctx)
        self.store = self.port("settings", SettingsStore)
        self.subscriptions: list[Subscription[Any]] = []
        self.offers = self._subscribe(SettingOffered)
        self.holds = self._subscribe(ScreenHeld)
        self.actions = self._subscribe(PanelAction)
        self.closings = self._subscribe(PanelClosed)
        self.requests = self._subscribe(SettingRequested)
        self.resets = self._subscribe(Resetting)
        self.capabilities = CapabilityHost(self)
        self.capabilities.add(OPEN, self._open_by_cloud)
        self.own = SettingsHost(self)  # its own lines: the robot's name and software
        about = SettingKind.INFO, SettingSection.ABOUT
        self.own.add(Setting("settings.name", *about, "Name", self.robot.display_name))
        self.own.add(Setting("settings.software", *about, "Software", software_version()))
        self.settings: dict[str, Setting] = {}  # by name, in the order first offered
        self._schemas: dict[str, Schema] = {}
        self.kept: dict[str, Any] = {}
        self.is_open = False
        self.resetting = False  # a factory reset began: the panel stays closed

    def _subscribe(self, kind: type[M]) -> Subscription[M]:
        subscription = self.bus.subscribe(kind)
        self.subscriptions.append(subscription)
        return subscription

    async def start(self) -> None:
        self.kept = await self.store.load()
        for name, value in list(self.kept.items()):
            try:
                self.bus.publish(SettingChanged(name, value))
            except ValueError:
                self.log.warning("the value kept for %s is not plain JSON: forgotten", name)
                del self.kept[name]
        self.own.offer()
        self.capabilities.offer()

    async def run(self) -> None:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self._follow(self.offers, self._on_offer))
            tasks.create_task(self._follow(self.holds, self._on_hold))
            tasks.create_task(self._follow(self.actions, self._on_action))
            tasks.create_task(self._follow(self.closings, self._on_closed))
            tasks.create_task(self._follow(self.requests, self._on_request))
            tasks.create_task(self._follow(self.resets, self._on_reset))
            tasks.create_task(self.capabilities.serve())
            tasks.create_task(self.own.serve())

    async def stop(self) -> None:
        for subscription in self.subscriptions:
            subscription.close()
        self.capabilities.close()
        self.own.close()

    @staticmethod
    async def _follow(
        subscription: Subscription[M], handle: Callable[[M], Awaitable[None]]
    ) -> None:
        async for message in subscription:
            await handle(message)

    # ---- what the modules offer ----

    async def _on_offer(self, message: SettingOffered) -> None:
        setting = message.setting
        if not setting.name.startswith(message.owner + "."):
            self.log.error("%s offers setting %s: not its own", message.owner, setting.name)
            return
        try:
            item_of(setting)
        except ValueError as error:
            self.log.error("%s", error)
            return
        previous = self.settings.get(setting.name)
        if setting.schema is not None and (previous is None or previous.schema != setting.schema):
            self._schemas[setting.name] = Schema(f"{setting.name} schema", setting.schema)
        self.settings[setting.name] = setting
        if self.is_open:
            self._show()

    # ---- the panel ----

    async def _on_hold(self, _: ScreenHeld) -> None:
        self._open()

    async def _open_by_cloud(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        self._open()
        return {"open": self.is_open}

    def _open(self) -> None:
        if self.resetting or self.resets.pending:  # one may wait to be heard, at start
            return
        if not self.is_open:
            self.is_open = True
            self.log.info("the settings are open")
            self.bus.publish(SettingsOpened())
        self._show()

    def _show(self) -> None:
        sections = []
        for section in SettingSection:
            items = [
                item_of(setting) for setting in self.settings.values() if setting.section is section
            ]
            if items:
                sections.append(PanelSection(section.value, tuple(items)))
        self.bus.publish(PanelRequest(self.name, Panel(PANEL, TITLE, tuple(sections))))

    def _close(self) -> None:
        if self.is_open:
            self.is_open = False
            self.log.info("the settings are closed")
            self.bus.publish(PanelRequest(self.name, None))

    async def _on_reset(self, _: Resetting) -> None:
        self.resetting = True
        self._close()

    async def _on_closed(self, message: PanelClosed) -> None:
        if message.panel == PANEL:
            self._close()

    async def _on_action(self, action: PanelAction) -> None:
        if action.panel != PANEL or not self.is_open:
            return
        setting = self.settings.get(action.item)
        if setting is None or setting.kind is SettingKind.INFO:
            self.log.warning("the settings have no item %r to act on", action.item)
            return
        if setting.kind is SettingKind.ACTION:
            self.bus.publish(SettingPressed(setting.name))
            if setting.closes:
                self._close()
            return
        await self._choose(setting, action.value)

    async def _on_request(self, request: SettingRequested) -> None:
        setting = self.settings.get(request.name)
        if setting is None or setting.kind is not SettingKind.VALUE:
            self.log.warning("the settings have no value %r to set", request.name)
            return
        await self._choose(setting, request.value)

    async def _choose(self, setting: Setting, value: Any) -> None:
        """The owner chose ``value`` for ``setting``, on the panel or elsewhere: keep it,
        and pass it on to its module."""
        problem = self._schemas[setting.name].problem(value)
        if problem is not None:
            self.log.warning("setting %s: refused %r: %s", setting.name, value, problem)
            if self.is_open:
                self._show()  # the panel shows the value as it is
            return
        # The panel shows the change at once, as the owner's screen already does, so that
        # no state sent before the module takes it puts the old value back on screen. The
        # module offers the setting again if it cannot take the value.
        self.settings[setting.name] = dataclasses.replace(setting, value=value)
        if self.is_open:
            self._show()
        self.kept[setting.name] = value
        try:
            await self.store.save(self.kept)
        except OSError as error:
            self.log.error("cannot keep the robot's settings: %s", error)
        self.bus.publish(SettingChanged(setting.name, value))
