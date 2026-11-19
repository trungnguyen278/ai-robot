# SPDX-License-Identifier: MIT
"""The factory reset of the robot's image, through systemd, the system's service manager
(docs/decisions/0023-factory-reset.md).

The robot asks systemd, on D-Bus, to start ``factory-reset.target``, as systemd's design
asks of an interface that offers a factory reset (systemd.io/FACTORY_RESET, read
2026-11-19). The image's units then keep the request across a restart and restart the
system; early in the next boot, the system erases the data partition and clears the
TPM, so the robot starts as a new one. The user who runs the robot must be allowed to
start that target through polkit.

Before it asks, the robot reads the system's factory reset mode from systemd's Varlink
service (systemd-factory-reset(8), read 2026-11-19), and checks that it did not ask in
an earlier boot. A target that only restarts the system, with no request for the next
boot, as Ubuntu's does on a computer that boots without EFI, such as the Pi, would
otherwise restart it again and again.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from dbus_fast import Message, MessageType

from ai_robot.adapters import varlink
from ai_robot.adapters.dbus import Owner, call, connect, watch
from ai_robot.adapters.files import reset_record
from ai_robot.adapters.files.reset_record import Record
from ai_robot.ports.factory_reset import FactoryReset, Restart

log = logging.getLogger(__name__)

# systemd's D-Bus API (org.freedesktop.systemd1(5), read 2026-11-19).
SYSTEMD = "org.freedesktop.systemd1"
SYSTEMD_PATH = "/org/freedesktop/systemd1"
MANAGER = f"{SYSTEMD}.Manager"
TARGET = "factory-reset.target"  # systemd's own name for it (systemd.io/FACTORY_RESET)
DONE = "done"  # a job's result once it succeeded (JobRemoved)

# systemd's Varlink interface for the factory reset, and the modes it reports
# (systemd-factory-reset(8); v259's src/shared/factory-reset.c; read 2026-11-19).
FACTORY_RESET = "io.systemd.FactoryReset"
GOES_ON = {"unspecified", "off", "pending"}  # no reset in this boot; one may be requested
REFUSED = {
    "unsupported": "the system offers no factory reset",
    "on": "the system's factory reset left the robot's data",  # this boot is the reset's
    "complete": "the system's factory reset left the robot's data",
}


class SystemdFactoryReset(FactoryReset):
    """Keeps that a reset began in the JSON file ``record`` (``~`` is the user's home),
    on the data partition, which the reset erases with the rest; asks systemd, on the
    D-Bus ``bus`` (``system``, or another bus's address, as the tests use), to start the
    image's factory reset, once its Varlink service at the socket ``varlink`` says the
    system can, in this boot, whose ID the file ``boot_id`` holds."""

    def __init__(self, *, record: str, bus: str, varlink: str, boot_id: str) -> None:
        self.record = reset_record.path_of(record)
        for name, value in (("bus", bus), ("varlink", varlink), ("boot_id", boot_id)):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must not be empty")
        self.bus_address = bus
        self.varlink = varlink
        self.boot_id = Path(boot_id)

    async def pending(self) -> bool:
        return await asyncio.to_thread(reset_record.read, self.record) is not None

    async def begin(self) -> None:
        await asyncio.to_thread(reset_record.write, self.record, Record())

    async def erase(self) -> Restart:
        """Ask systemd to start ``factory-reset.target``, and wait until its job is done.
        ``RuntimeError`` if the system cannot reset the robot, or did not when asked."""
        mode = await self._mode()
        if mode not in GOES_ON:
            raise RuntimeError(REFUSED.get(mode, "the system's factory reset is in no known mode"))
        boot = (await asyncio.to_thread(self.boot_id.read_text, encoding="ascii")).strip()
        record = await asyncio.to_thread(reset_record.read, self.record)
        if record is not None and record.boot not in (None, boot):
            raise RuntimeError("the system restarted without erasing the robot")
        await asyncio.to_thread(reset_record.write, self.record, Record(boot))
        await self._start()
        log.info("the system resets the robot at its next start")
        return Restart.SYSTEM

    async def _mode(self) -> str:
        """The system's factory reset mode; empty if it gave none."""
        try:
            reply = await varlink.call(self.varlink, f"{FACTORY_RESET}.GetFactoryResetMode")
        except (OSError, varlink.VarlinkError) as error:
            raise RuntimeError("the system's factory reset cannot be reached") from error
        mode = reply.get("mode")
        return mode if isinstance(mode, str) else ""

    async def _start(self) -> None:
        """Start ``factory-reset.target`` and wait until its job ends; ``RuntimeError``
        unless it is done."""
        bus = await connect(self.bus_address)
        systemd = Owner(SYSTEMD)
        ended: dict[str, str] = {}  # job -> result, as systemd says each job ended
        changed = asyncio.Event()

        def on_message(message: Message) -> None:
            if systemd.changed(message) or not systemd.sent(message):
                return
            if (
                message.message_type is MessageType.SIGNAL
                and message.interface == MANAGER
                and message.member == "JobRemoved"
                and message.path == SYSTEMD_PATH
            ):
                _, job, _, result = message.body
                ended[job] = result
                changed.set()

        bus.add_message_handler(on_message)
        try:
            await systemd.follow(bus)
            await watch(
                bus,
                f"type='signal',sender='{SYSTEMD}',interface='{MANAGER}',"
                f"member='JobRemoved',path='{SYSTEMD_PATH}'",
            )
            await call(bus, SYSTEMD, SYSTEMD_PATH, MANAGER, "Subscribe")  # or none is sent
            (job,) = await call(
                bus, SYSTEMD, SYSTEMD_PATH, MANAGER, "StartUnit", "ss", [TARGET, "replace"]
            )
            while job not in ended:
                changed.clear()
                await changed.wait()
        finally:
            bus.disconnect()
        if ended[job] != DONE:
            log.error("the job that starts the system's factory reset ended: %s", ended[job])
            raise RuntimeError("the system could not start its factory reset")
