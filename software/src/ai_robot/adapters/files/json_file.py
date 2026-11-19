# SPDX-License-Identifier: MIT
"""JSON files written whole or not at all, for the robot's state."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def read(path: Path) -> Any:
    """The JSON value in ``path``; ``None`` if there is no such file. ``OSError`` or
    ``ValueError`` if it cannot be read."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def write(path: Path, value: Any) -> None:
    """Write ``value`` as JSON to ``path``: in a new file beside the old one, then put in
    its place, so that a power cut leaves one or the other, never half of one. It is on
    the disk once this returns, with the folders made for it."""
    changed = [path.parent]  # the folders whose entries change: the file's, and each one
    while not changed[-1].exists():  # that holds a folder made here
        changed.append(changed[-1].parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False)
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    for folder in changed:
        sync_folder(folder)


def sync_folder(folder: Path) -> None:
    """Put the entries of ``folder`` on the disk: a file made, renamed or removed in it is
    not there until the folder is synced too (fsync(2), Linux man-pages 6.19, read
    2026-11-19). Only POSIX systems open a folder to sync it."""
    if os.name != "posix":
        return
    handle = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)
