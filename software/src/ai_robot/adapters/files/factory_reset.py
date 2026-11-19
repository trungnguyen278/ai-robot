# SPDX-License-Identifier: MIT
"""The factory reset of a robot whose state is in files: a development computer, or a
Pi that runs the robot from its repository (docs/decisions/0023-factory-reset.md).

It erases the robot's own files and folders that its configuration lists (the robot's
state folder, and the key file of a ``file`` identity), and the Wi-Fi profiles set-up
made, then starts the robot again in this process. It clears no TPM: a robot whose
identity is in its TPM is reset by its image (``systemd/``). It leaves the system's time
zone, which is the computer's: set-up sets it again from the new owner's phone.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

from ai_robot.adapters.files import json_file, reset_record
from ai_robot.adapters.files.reset_record import Record
from ai_robot.ports.factory_reset import FactoryReset, Restart

log = logging.getLogger(__name__)


class FileFactoryReset(FactoryReset):
    """Keeps that a reset began in the JSON file ``record``, and erases each path of
    ``erase`` (``~`` is the user's home); ``wifi`` also forgets the profiles set-up made
    in NetworkManager, on the D-Bus ``bus``: ``system``, or another bus's address, as the
    tests use.

    It erases only what the robot owns: what is in a folder named as the one that holds
    the record, such as ``~/.local/state/ai-robot`` and ``~/.config/ai-robot``, the
    robot's own folders under the XDG base directories (XDG Base Directory Specification
    0.8: ``$XDG_CONFIG_HOME/subdir/filename``). Never the home folder, nor a folder that
    holds it, even when the robot's user has the robot's name.

    The record goes last, so that an erase cut short starts again at the next start. A
    folder to erase that holds it stays, empty.
    """

    def __init__(self, *, record: str, erase: list[str], wifi: bool, bus: str) -> None:
        self.record = reset_record.path_of(record)
        if _is_or_holds_home(self.record.parent):
            raise ValueError("record must be in a folder of the robot's own")
        if not isinstance(erase, list) or not all(isinstance(path, str) and path for path in erase):
            raise ValueError("erase must list paths")
        self.paths = [Path(path).expanduser() for path in erase]
        if not all(self._owned(path) for path in self.paths):
            raise ValueError("erase must list only the robot's own files and folders")
        self.wifi = wifi
        self.bus_address = bus

    def _owned(self, path: Path) -> bool:
        if not path.is_absolute() or ".." in path.parts:
            return False
        name = self.record.parent.name
        return any(
            folder.name == name and not _is_or_holds_home(folder)
            for folder in (path, *path.parents)
        )

    async def pending(self) -> bool:
        return await asyncio.to_thread(reset_record.read, self.record) is not None

    async def begin(self) -> None:
        await asyncio.to_thread(reset_record.write, self.record, Record())

    async def erase(self) -> Restart:
        if self.wifi:
            await self._forget_networks()
        await asyncio.to_thread(self._erase)
        log.info("the robot is erased")
        return Restart.PROCESS

    async def _forget_networks(self) -> None:
        from ai_robot.adapters.dbus import connect
        from ai_robot.adapters.networkmanager.profiles import forget_made

        bus = await connect(self.bus_address)
        try:
            gone = await forget_made(bus)
        finally:
            bus.disconnect()
        log.info("forgot %d Wi-Fi network(s) set-up joined", gone)

    def _erase(self) -> None:
        for path in self.paths:
            self._remove(path)
        if hasattr(os, "sync"):  # what is erased is on the disk before the record goes
            os.sync()
        self.record.unlink(missing_ok=True)
        json_file.sync_folder(self.record.parent)

    def _remove(self, path: Path) -> None:
        """Remove ``path``, but not the record or the folders that hold it. A link goes,
        and never what it points to."""
        if path == self.record:
            return
        if path.is_symlink() or not path.is_dir():
            path.unlink(missing_ok=True)
        elif path in self.record.parents:
            for entry in path.iterdir():
                self._remove(entry)
        else:
            shutil.rmtree(path)


def _is_or_holds_home(folder: Path) -> bool:
    return Path.home().is_relative_to(folder)
