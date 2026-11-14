# SPDX-License-Identifier: MIT
"""The registries and the shipped configuration are consistent."""

from __future__ import annotations

import dataclasses
import importlib.util
import re
import tomllib
from collections import defaultdict
from pathlib import Path

import pytest

from ai_robot import messages
from ai_robot.adapters import ADAPTERS, create_adapter
from ai_robot.app import App
from ai_robot.core.bus import Message
from ai_robot.core.config import ConfigError, available_profiles, load_config
from ai_robot.core.factory import import_object
from ai_robot.modules import MODULES
from ai_robot.ports import PORT_TYPES

# Adapters whose library is installed only where they run (pyproject.toml's extras, and
# dependencies for Linux only).
EXTRAS = {
    ("identity", "tpm"): "tpm2_pytss",
    ("setup_link", "bluez"): "dbus_fast",
    ("network", "networkmanager"): "dbus_fast",
    ("time_zone", "timedated"): "dbus_fast",
    ("display_power", "helper"): "fcntl",  # its helper's ABI is Linux's
}
PROFILES = sorted(
    path.stem for path in (Path(__file__).parents[1] / "config" / "profiles").glob("*.toml")
)


def installed(port: str, adapter: str) -> bool:
    library = EXTRAS.get((port, adapter))
    return library is None or importlib.util.find_spec(library) is not None


def test_every_adapter_implements_its_port() -> None:
    assert set(ADAPTERS) == set(PORT_TYPES)
    for port, adapters in ADAPTERS.items():
        for name, path in adapters.items():
            if installed(port, name):
                assert issubclass(import_object(path), PORT_TYPES[port]), path


def test_an_adapter_without_its_library_is_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(ADAPTERS["identity"], "elsewhere", "no_such_library.keys:Identity")
    with pytest.raises(ConfigError, match="needs no_such_library, which is not installed"):
        create_adapter("identity", {"adapter": "elsewhere"})


def test_every_port_has_exactly_one_owner() -> None:
    owners: dict[str, list[str]] = defaultdict(list)
    for name, path in MODULES.items():
        for port in import_object(path).owns:
            owners[port].append(name)
    assert set(owners) == set(PORT_TYPES)
    assert {port: names for port, names in owners.items() if len(names) > 1} == {}


def test_messages_are_frozen_dataclasses() -> None:
    classes = [
        value
        for value in vars(messages).values()
        if isinstance(value, type) and issubclass(value, Message) and value is not Message
    ]
    assert classes
    for cls in classes:
        assert dataclasses.is_dataclass(cls), cls.__name__
        assert cls.__dataclass_params__.frozen, cls.__name__  # type: ignore[attr-defined]


def test_the_profiles_ship(shipped_config: Path) -> None:
    assert {"desktop", "pi"} <= set(available_profiles(shipped_config)) == set(PROFILES)


@pytest.mark.parametrize("profile", PROFILES)
def test_a_shipped_profile_builds(shipped_config: Path, profile: str) -> None:
    config = load_config(profile, shipped_config)
    missing = [
        f"{port} ({table['adapter']})"
        for port, table in config["ports"].items()
        if not installed(port, table["adapter"])
    ]
    if missing:
        pytest.skip(f"needs libraries not installed here: {', '.join(missing)}")
    App(config).build()


# What the configuration cannot ship, because it belongs to one robot or one machine.
MACHINE_SPECIFIC = {
    ("cloud", "websocket"): {"server": "https://cloud.example"},
    ("identity", "file"): {"path": "identity.json"},
    ("daylight", "solar"): {"latitude": 21.03, "longitude": 105.85},
}


def test_every_adapter_builds_from_the_shipped_configuration(shipped_config: Path) -> None:
    """The configuration holds every setting of every adapter, even of those no profile
    selects yet: nothing has a default hidden in the code."""
    ports = load_config("desktop", shipped_config)["ports"]
    for port, adapters in ADAPTERS.items():
        for name in adapters:
            if not installed(port, name):
                continue
            table = {**ports[port], "adapter": name}
            extra = MACHINE_SPECIFIC.get((port, name))
            if extra is not None:
                table[name] = {**table.get(name, {}), **extra}
            create_adapter(port, table)


def uncommented(text: str) -> str:
    """An example configuration with its commented-out settings switched on: lines such
    as "# [table]" and "# key = value", not the prose around them."""
    setting = re.compile(r"\[[\w.]+\]$|[A-Za-z_]\w* = ")
    return "\n".join(
        line[2:] if line.startswith("# ") and setting.match(line[2:]) else line
        for line in text.splitlines()
    )


def test_the_local_example_works_as_a_local_toml(shipped_config: Path) -> None:
    """Every setting in local.example.toml exists, with a value of the right type, so
    that the template never leads anyone astray."""
    local = uncommented((shipped_config / "local.example.toml").read_text(encoding="utf-8"))
    assert sum(len(table) for table in tomllib.loads(local).values()) >= 5  # found some
    (shipped_config / "local.toml").write_text(local, encoding="utf-8")
    config = load_config("desktop", shipped_config)
    for port, table in config["ports"].items():
        if not installed(port, table["adapter"]):  # it builds where its library is (CI)
            table["adapter"] = "sim"
    App(config).build()
