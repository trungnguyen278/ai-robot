# SPDX-License-Identifier: MIT
"""The record that a factory reset began (docs/decisions/0023-factory-reset.md).

It is a JSON object whose ``resetting`` is true: ``{"resetting": true}``. The ``systemd``
adapter adds ``boot``, the boot in which it asked the system to erase the robot. A file
that holds anything else says nothing: the robot logs it, by its path, and starts as it
is, since only its own record may start an erase.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ai_robot.adapters.files import json_file
from ai_robot.core.data import read_json

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Record:
    """A reset began; ``boot`` is the boot in which the system was asked to erase the
    robot, if it was."""

    boot: str | None = None


def path_of(record: str) -> Path:
    """The record's path, from the configuration: absolute, or in the home folder (``~``).
    ``ValueError`` otherwise."""
    if not isinstance(record, str) or not record:
        raise ValueError("record must be the path of a file")
    path = Path(record).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("record must be an absolute path, or one in the home folder (~)")
    return path


def read(path: Path) -> Record | None:
    """The reset that ``path`` says began, or ``None``."""
    try:
        value = read_json(path.read_bytes())  # a file of any content: nesting is bounded
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        log.warning("%s cannot be read: it starts no reset", path)
        return None
    record = _record(value)
    if record is None:
        log.warning("%s is not a reset's record: it starts no reset", path)
    return record


def _record(value: object) -> Record | None:
    if not isinstance(value, dict) or set(value) - {"boot"} != {"resetting"}:
        return None
    if value["resetting"] is not True:  # not 1, which equals True
        return None
    boot = value.get("boot")
    if "boot" in value and not (isinstance(boot, str) and boot):
        return None
    return Record(boot)


def write(path: Path, record: Record) -> None:
    """Keep ``record`` in ``path``, on the disk before this returns."""
    value: dict[str, object] = {"resetting": True}
    if record.boot is not None:
        value["boot"] = record.boot
    json_file.write(path, value)
