# SPDX-License-Identifier: MIT
"""Reset: the robot's factory reset, from its settings (docs/decisions/0023-factory-reset.md)."""

from __future__ import annotations

import asyncio

from ai_robot.core.checks import positive
from ai_robot.core.module import Module, ModuleContext
from ai_robot.core.settings import SettingsHost
from ai_robot.messages import (
    HomeLeft,
    ResetStage,
    Resetting,
    RestartRequested,
    SceneRequest,
    ScreenPriority,
    Setting,
    SettingKind,
    SettingSection,
)
from ai_robot.ports.factory_reset import FactoryReset, Restart

ACTION = "reset.factory"
SCENE = "reset"  # the screen's scene, which says how the reset goes (protocol/screen)


class ResetModule(Module):
    """Erases the robot when its owner asks, in its settings, and starts it again as a
    new robot.

    - **Asking.** The settings' About section ends with "Reset to factory settings". A
      second screen says what goes and that it cannot be undone, with Cancel and Erase.
    - **Leaving.** Once the owner erases, the robot keeps that it is resetting
      (``factory_reset``), then ``cloud`` takes it out of its home (``Resetting``,
      ``HomeLeft``), so that the app no longer lists it. Set-up and the settings close
      and stay closed.
    - **Erasing.** It erases even when it could not leave, as it must not depend on the
      network (ETSI EN 303 645, provision 5.11-1); its screen first says, for ``note``
      seconds, to remove the robot in the app. Then it starts again (``RestartRequested``),
      or the system restarts it.
    - **Cut short.** A reset cut short, by a power cut for one, goes on at the next start.
    """

    owns = ("factory_reset",)
    needs = ("cloud",)  # which says whether the robot left its home (HomeLeft)

    def __init__(self, ctx: ModuleContext, *, note: float) -> None:
        super().__init__(ctx)
        self.note = positive("note", note)
        self.reset = self.port("factory_reset", FactoryReset)
        self._left = self.bus.subscribe(HomeLeft)
        self.settings = SettingsHost(self)
        self.settings.add(
            Setting(
                ACTION,
                SettingKind.ACTION,
                SettingSection.ABOUT,
                "Reset to factory settings",
                help="Erases the robot, to give it away or set it up anew",
                confirm="Reset to factory settings?",
                confirm_lines=(
                    "The robot forgets its Wi-Fi networks, its name and its settings, "
                    "and leaves your home.",
                    "It then starts as a new robot, to set up again.",
                    "This cannot be undone.",
                ),
                confirm_label="Erase",
                closes=True,
            ),
            self._on_erase,
        )
        self._asked = asyncio.Event()
        self._began = False  # what says that a reset began is kept

    async def start(self) -> None:
        if await self.reset.pending():
            self.log.warning("a factory reset was cut short: it goes on")
            self._began = True
            self._asked.set()
            self._stage(ResetStage.LEAVING)  # before any module runs: none opens set-up
        else:
            self.settings.offer()

    async def run(self) -> None:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self.settings.serve())
            tasks.create_task(self._go())

    async def stop(self) -> None:
        self.settings.close()
        self._left.close()

    async def _on_erase(self, _: None) -> None:
        self._asked.set()

    async def _go(self) -> None:
        await self._asked.wait()
        if not self._began:
            self.log.warning("the owner resets the robot to its factory settings")
            await self.reset.begin()  # before it leaves: a reset cut short goes on
            self._began = True
            self._stage(ResetStage.LEAVING)
        left = await self._left.get()
        if not left.left:
            self.log.warning("the robot could not leave its home: the app may still list it")
            self._stage(ResetStage.NOT_LEFT)
            await asyncio.sleep(self.note)
        self._stage(ResetStage.ERASING)
        restart = await self.reset.erase()
        if restart is Restart.PROCESS:
            self.bus.publish(RestartRequested())
        else:
            self.log.info("the robot waits for the system to restart it")

    def _stage(self, stage: ResetStage) -> None:
        self.bus.publish(Resetting(stage))
        self.bus.publish(
            SceneRequest(
                self.name,
                SCENE,
                {"state": stage.value},
                priority=ScreenPriority.ALERT,
                keep_awake=True,
            )
        )
