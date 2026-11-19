# SPDX-License-Identifier: MIT
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ai_robot.__main__ import main
from ai_robot.app import App


def test_runs_the_desktop_profile(shipped_config: Path) -> None:
    args = ["--profile", "desktop", "--config-dir", str(shipped_config), "--duration", "0.2"]
    assert main(args) == 0


def test_a_robot_reset_starts_again(
    shipped_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reset cut short goes on as the robot starts, then a new robot runs."""
    state = tmp_path / "state"
    record = state / "resetting.json"
    state.mkdir()
    record.write_text('{"resetting": true}', encoding="utf-8")
    (state / "settings.json").write_text("{}", encoding="utf-8")
    local = f"""
[ports.screen.web]
port = 0
[ports.factory_reset]
adapter = "files"
[ports.factory_reset.files]
record = '{record}'
erase = ['{state}']
[modules.reset]
note = 0.01
"""
    (shipped_config / "local.toml").write_text(local, encoding="utf-8")
    runs: list[App] = []
    run = App.run

    async def counted(app: App, stop: asyncio.Event) -> None:
        runs.append(app)
        if len(runs) == 2:
            stop.set()  # the new robot started: enough
        await run(app, stop)

    monkeypatch.setattr(App, "run", counted)
    # The duration only bounds a robot that never starts again.
    args = ["--profile", "desktop", "--config-dir", str(shipped_config), "--duration", "60"]
    assert main(args) == 0
    assert [app.restart for app in runs] == [True, False]
    assert list(state.iterdir()) == []


def test_unknown_profile(shipped_config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--profile", "nope", "--config-dir", str(shipped_config)]) == 2
    assert "unknown profile 'nope'" in capsys.readouterr().err


def test_log_must_be_a_table(shipped_config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (shipped_config / "local.toml").write_text('log = "DEBUG"\n', encoding="utf-8")
    assert main(["--config-dir", str(shipped_config)]) == 2
    assert "[log] must be a table" in capsys.readouterr().err


@pytest.mark.parametrize("duration", ["nan", "-1", "soon"])
def test_duration_must_be_a_number_of_seconds(duration: str) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--duration", duration])
    assert caught.value.code == 2


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--version"])
    assert caught.value.code == 0
    assert capsys.readouterr().out.startswith("ai-robot ")
