# SPDX-License-Identifier: MIT
"""The application: builds the robot from the configuration and runs it."""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import Coroutine, Iterable, Mapping
from typing import Any

from ai_robot.adapters import create_adapter
from ai_robot.core.bus import Bus, Subscription
from ai_robot.core.config import ConfigError, get_table
from ai_robot.core.factory import create, import_object
from ai_robot.core.module import Module, ModuleContext, RobotInfo
from ai_robot.messages import RestartRequested
from ai_robot.modules import MODULES
from ai_robot.ports import Port

log = logging.getLogger(__name__)

# The forms the protocols give a device's kind and model: $defs/code and $defs/name in the
# schemas of ai-platform's protocols/cloud and protocols/setup.
DEVICE_KIND = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
DEVICE_MODEL = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class ModuleError(Exception):
    """A module failed, so the robot was stopped."""


class App:
    """The robot: the enabled modules, the adapters of the ports they own, and the bus.

    An App runs once; create a new one to run the robot again. A module may ask for that
    (``RestartRequested``): the App then stops as if stopped, with ``restart`` set.
    """

    def __init__(self, config: Mapping[str, Any]) -> None:
        self.config = config
        self.bus = Bus()
        self.robot = RobotInfo(display_name="", kind="", model="")
        self.shutdown_timeout = 0.0  # seconds for each step of the shutdown, once built
        self.adapters: dict[str, Port] = {}
        self.modules: list[Module] = []
        self.restart = False  # a module asked to start the robot again
        self._built = False
        self._ran = False

    def build(self) -> None:
        """Create the enabled modules and the adapters of the ports they own.

        Any mistake in the configuration raises ``ConfigError`` here, before any
        hardware is touched.
        """
        if self._built:
            return
        robot_table = get_table(self.config, "robot")
        app_table = get_table(self.config, "app")
        modules_table = get_table(self.config, "modules")
        ports_table = get_table(self.config, "ports")
        name = robot_table.get("display_name")
        if not isinstance(name, str) or not name:
            raise ConfigError("robot.display_name must be the name shown to people")
        kind, model = robot_table.get("kind"), robot_table.get("model")
        if not isinstance(kind, str) or not DEVICE_KIND.match(kind):
            raise ConfigError(f"robot.kind must be a kind of device, such as robot, got {kind!r}")
        if not isinstance(model, str) or not DEVICE_MODEL.match(model):
            raise ConfigError(f"robot.model must be its model's name, got {model!r}")
        robot = RobotInfo(display_name=name, kind=kind, model=model)
        timeout = app_table.get("shutdown_step_timeout")
        if not (
            isinstance(timeout, int | float)
            and not isinstance(timeout, bool)
            and math.isfinite(timeout)
            and timeout > 0
        ):
            raise ConfigError(
                f"app.shutdown_step_timeout must be a positive number of seconds, got {timeout!r}"
            )

        enabled = modules_table.get("enabled", [])
        if not isinstance(enabled, list) or not all(isinstance(name, str) for name in enabled):
            raise ConfigError("modules.enabled must be a list of module names")
        classes: dict[str, type[Module]] = {}
        owners: dict[str, str] = {}
        for name in enabled:
            if name not in MODULES:
                known = ", ".join(sorted(MODULES))
                raise ConfigError(f"modules.enabled: unknown module {name!r} (known: {known})")
            if name in classes:
                raise ConfigError(f"modules.enabled: {name!r} is listed twice")
            classes[name] = cls = import_object(MODULES[name])
            for port in cls.owns:
                if port in owners:
                    raise ConfigError(
                        f"port {port!r} is claimed by both {owners[port]!r} and {name!r}"
                    )
                owners[port] = name

        for name, cls in classes.items():
            missing = [needed for needed in cls.needs if needed not in classes]
            if missing:
                raise ConfigError(
                    f"modules.enabled: {name!r} needs {', '.join(map(repr, missing))}"
                )

        adapters: dict[str, Port] = {}
        for port, owner in owners.items():
            if port not in ports_table:
                raise ConfigError(f"[ports.{port}] is missing; module {owner!r} needs it")
            adapters[port] = create_adapter(port, get_table(ports_table, port, f"ports.{port}"))

        modules: list[Module] = []
        for name, cls in classes.items():
            ctx = ModuleContext(
                name=name,
                bus=self.bus,
                ports={port: adapters[port] for port in cls.owns},
                robot=robot,
                log=logging.getLogger(f"ai_robot.modules.{name}"),
            )
            settings = get_table(modules_table, name, f"modules.{name}")
            modules.append(create(cls, f"modules.{name}", ctx, **settings))

        self.robot, self.adapters, self.modules = robot, adapters, modules
        self.shutdown_timeout = float(timeout)
        self._built = True

    async def run(self, stop: asyncio.Event) -> None:
        """Run the robot until ``stop`` is set, or a module asks for a restart.

        Setting ``stop`` also interrupts the startup (opening the adapters, starting the
        modules). If an adapter or a module fails, the robot is stopped the same way and
        the error is raised (``ModuleError`` for a module). Whatever happens, every
        started module is stopped and every opened adapter closed, in reverse order, each
        step within ``shutdown_step_timeout`` seconds (``[app]`` in the configuration).
        """
        if self._ran:
            raise RuntimeError("an App runs only once; create a new one")
        self._ran = True
        self.build()
        opened: list[Port] = []
        started: list[Module] = []
        timeout = self.shutdown_timeout
        halt = asyncio.Event()  # stopped, or asked to restart
        restarts = self.bus.subscribe(RestartRequested)
        watchers = [
            asyncio.create_task(self._until(stop, halt), name="stop"),
            asyncio.create_task(self._restart_on(restarts, halt), name="restart"),
        ]
        try:
            startup = asyncio.create_task(self._start(opened, started), name="startup")
            if not await _until_stopped([startup], halt, timeout):
                return
            log.info("running: %s", ", ".join(module.name for module in self.modules))
            tasks = [
                asyncio.create_task(_run_module(module), name=f"module {module.name}")
                for module in self.modules
            ]
            if await _until_stopped(tasks, halt, timeout):
                await halt.wait()  # every module has returned: wait until stopped
        finally:
            await _cancel(watchers, timeout)
            restarts.close()
            for module in reversed(started):
                await _cleanup(module.stop(), f"stopping module {module.name!r}", timeout)
            for adapter in reversed(opened):
                await _cleanup(adapter.close(), f"closing {type(adapter).__name__}", timeout)
            log.info("stopped")

    @staticmethod
    async def _until(stop: asyncio.Event, halt: asyncio.Event) -> None:
        await stop.wait()
        halt.set()

    async def _restart_on(
        self, restarts: Subscription[RestartRequested], halt: asyncio.Event
    ) -> None:
        await restarts.get()
        log.info("restarting, as asked")
        self.restart = True
        halt.set()

    async def _start(self, opened: list[Port], started: list[Module]) -> None:
        """Open every adapter, then start every module, recording each success."""
        for adapter in self.adapters.values():
            await adapter.open()
            opened.append(adapter)
        for module in self.modules:
            try:
                await module.start()
            except Exception as error:
                raise ModuleError(f"module {module.name!r} failed to start") from error
            started.append(module)


async def _run_module(module: Module) -> None:
    try:
        await module.run()
    except Exception as error:
        raise ModuleError(f"module {module.name!r} failed") from error
    log.info("module %r has nothing more to do", module.name)


async def _until_stopped(
    tasks: list[asyncio.Task[None]], stop: asyncio.Event, timeout: float
) -> bool:
    """Wait until every task has finished (return True) or ``stop`` is set (False).

    If a task fails, its error is raised. On the way out, the tasks still running are
    cancelled, and given ``timeout`` seconds to finish.
    """
    stopping = asyncio.create_task(stop.wait(), name="stop")
    pending = set(tasks)
    try:
        while pending and not stopping.done():
            done, pending = await asyncio.wait(
                {stopping, *pending}, return_when=asyncio.FIRST_COMPLETED
            )
            pending.discard(stopping)
            errors: list[tuple[str, BaseException]] = []
            for task in done - {stopping}:
                if not task.cancelled() and (error := task.exception()) is not None:
                    errors.append((task.get_name(), error))
            for name, error in errors[1:]:
                log.error("%s also failed", name, exc_info=error)
            if errors:
                raise errors[0][1]
        return not stop.is_set()
    finally:
        await _cancel([stopping, *pending], timeout)


async def _cancel(tasks: Iterable[asyncio.Task[Any]], timeout: float) -> None:
    """Cancel ``tasks`` and wait up to ``timeout`` seconds for them to finish.

    Errors raised while they finish are logged. A task that ignores its cancellation is
    logged and left behind, so that the shutdown can go on.
    """
    running = [task for task in tasks if not task.done()]
    if not running:
        return
    for task in running:
        task.cancel()
    done, pending = await asyncio.wait(running, timeout=timeout)
    for task in done:
        if not task.cancelled() and task.exception() is not None:
            log.error("%s failed while stopping", task.get_name(), exc_info=task.exception())
    for task in pending:
        log.error("%s did not stop within %s s; leaving it behind", task.get_name(), timeout)


async def _cleanup(step: Coroutine[Any, Any, None], what: str, timeout: float) -> None:
    """Run one shutdown step for up to ``timeout`` seconds, logging its failure instead
    of raising it, so that the remaining steps still run."""
    task = asyncio.create_task(step, name=what)
    await asyncio.wait([task], timeout=timeout)
    if not task.done():
        task.cancel()
        task.add_done_callback(_consume)
        log.error("%s took more than %s s; moving on", what, timeout)
    elif not task.cancelled() and task.exception() is not None:
        log.error("error while %s", what, exc_info=task.exception())


def _consume(task: asyncio.Task[Any]) -> None:
    """Retrieve the outcome of a task nobody waits for, so asyncio does not warn about it."""
    if not task.cancelled():
        task.exception()
