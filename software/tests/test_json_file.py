# SPDX-License-Identifier: MIT
"""The robot's JSON files: written whole, and on the disk once written, with the folders
made for them (fsync(2))."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from ai_robot.adapters.files import json_file


def test_a_file_written_is_on_the_disk_with_the_folders_made_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state" / "robot" / "value.json"
    synced: list[tuple[Path, bool]] = []  # each folder synced, and whether the file was there
    monkeypatch.setattr(
        json_file, "sync_folder", lambda folder: synced.append((folder, path.exists()))
    )
    json_file.write(path, {"name": "Robot"})
    assert synced == [(path.parent, True), (tmp_path / "state", True), (tmp_path, True)]
    synced.clear()
    json_file.write(path, {"name": "Robbie"})
    assert synced == [(path.parent, True)]
    assert json_file.read(path) == {"name": "Robbie"}
    assert [entry.name for entry in path.parent.iterdir()] == ["value.json"]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux says which file a descriptor is")
def test_a_folder_is_synced_and_let_go(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    synced: list[str] = []
    fsync = os.fsync

    def recording(handle: int) -> None:
        synced.append(os.readlink(f"/proc/self/fd/{handle}"))
        fsync(handle)

    monkeypatch.setattr(os, "fsync", recording)
    open_before = len(os.listdir("/proc/self/fd"))
    json_file.sync_folder(tmp_path)
    assert synced == [str(tmp_path)]
    assert len(os.listdir("/proc/self/fd")) == open_before
