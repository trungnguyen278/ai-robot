# SPDX-License-Identifier: MIT
"""Adapters: the implementations of the ports.

The configuration selects one adapter per port (``adapter = "sim"``). Adapters are
grouped by technology, so the libraries of a technology are imported only when one of its
adapters is selected:
- ``sim/``: simulated;
- ``portaudio/``, with sounddevice: the microphones and the speaker;
- ``microwakeword/``, with pymicro-wakeword: wake words;
- ``silero/``, with pysilero-vad: voice;
- ``web/``, with aiohttp: the screen served to a browser;
- ``websocket/``, with aiohttp: the link to the cloud;
- ``keys/``: keys kept in software;
- ``tpm/``, with tpm2-pytss: keys in the robot's TPM (the ``tpm`` extra, on Linux);
- ``files/``: the robot's state in files;
- ``bluez/``, with dbus-fast: Bluetooth LE, for set-up (Linux);
- ``networkmanager/``, with dbus-fast: the robot's Wi-Fi (Linux);
- ``timedated/``, with dbus-fast: the robot's time zone (Linux);
- ``sun/``: sunrise and sunset;
- ``wake/``: the screen woken by the kiosk's helper, ``ai-robot-wake`` (Linux);
- later, Picamera2 or the body link.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ai_robot.core.config import ConfigError
from ai_robot.core.factory import create, import_object
from ai_robot.ports import PORT_TYPES, Port

# port -> adapter name -> "package.module:Class"
ADAPTERS: dict[str, dict[str, str]] = {
    "audio_in": {
        "sim": "ai_robot.adapters.sim.audio:SimAudioIn",
        "portaudio": "ai_robot.adapters.portaudio.audio:PortAudioIn",
    },
    "audio_out": {
        "sim": "ai_robot.adapters.sim.audio:SimAudioOut",
        "portaudio": "ai_robot.adapters.portaudio.audio:PortAudioOut",
    },
    "wake_word": {
        "sim": "ai_robot.adapters.sim.listening:SimWakeWord",
        "micro": "ai_robot.adapters.microwakeword.wake_word:MicroWakeWords",
    },
    "voice_activity": {
        "sim": "ai_robot.adapters.sim.listening:SimVoiceActivity",
        "silero": "ai_robot.adapters.silero.voice_activity:SileroVoiceActivity",
    },
    "camera": {"sim": "ai_robot.adapters.sim.camera:SimCamera"},
    "screen": {
        "sim": "ai_robot.adapters.sim.screen:SimScreen",
        "web": "ai_robot.adapters.web.screen:WebScreen",
    },
    "cloud": {
        "sim": "ai_robot.adapters.sim.cloud:SimCloud",
        "websocket": "ai_robot.adapters.websocket.cloud:WebSocketCloud",
    },
    "identity": {
        "sim": "ai_robot.adapters.sim.identity:SimIdentity",
        "file": "ai_robot.adapters.keys.identity:FileIdentity",
        "tpm": "ai_robot.adapters.tpm.identity:TpmIdentity",
    },
    "binding": {
        "sim": "ai_robot.adapters.sim.binding:SimBindingStore",
        "file": "ai_robot.adapters.files.binding:FileBindingStore",
    },
    "settings": {
        "sim": "ai_robot.adapters.sim.settings:SimSettingsStore",
        "file": "ai_robot.adapters.files.settings:FileSettingsStore",
    },
    "setup_link": {
        "sim": "ai_robot.adapters.sim.setup:SimSetupLink",
        "bluez": "ai_robot.adapters.bluez.setup:BluezSetupLink",
    },
    "network": {
        "sim": "ai_robot.adapters.sim.network:SimNetwork",
        "networkmanager": "ai_robot.adapters.networkmanager.network:NetworkManagerNetwork",
    },
    "daylight": {
        "fixed": "ai_robot.adapters.sun.daylight:FixedDaylight",
        "solar": "ai_robot.adapters.sun.daylight:SolarDaylight",
    },
    "display_power": {
        "sim": "ai_robot.adapters.sim.display_power:SimDisplayPower",
        "helper": "ai_robot.adapters.wake.client:HelperDisplayPower",
    },
    "time_zone": {
        "sim": "ai_robot.adapters.sim.time_zone:SimTimeZone",
        "timedated": "ai_robot.adapters.timedated.time_zone:TimedatedTimeZone",
    },
    "drive": {"sim": "ai_robot.adapters.sim.body:SimDriveBase"},
    "joints": {"sim": "ai_robot.adapters.sim.body:SimJoints"},
    "range": {"sim": "ai_robot.adapters.sim.body:SimRangeSensor"},
    "power": {"sim": "ai_robot.adapters.sim.body:SimPower"},
    "lights": {"sim": "ai_robot.adapters.sim.body:SimLights"},
    "safety": {"sim": "ai_robot.adapters.sim.body:SimSafety"},
}


def create_adapter(port: str, table: Mapping[str, Any]) -> Port:
    """Create the adapter selected by ``table``, the ``[ports.<port>]`` table.

    The adapter receives the table's keys as keyword arguments, except ``adapter`` and
    the sub-tables named after adapters; the keys of the sub-table named after the
    selected adapter are added on top.
    """
    adapters = ADAPTERS.get(port)
    if adapters is None:
        raise ConfigError(f"unknown port {port!r}")
    name = table.get("adapter")
    if not isinstance(name, str) or name not in adapters:
        raise ConfigError(
            f"ports.{port}.adapter: expected one of {', '.join(sorted(adapters))}, got {name!r}"
        )
    settings = {
        key: value for key, value in table.items() if key != "adapter" and key not in adapters
    }
    specific = table.get(name, {})
    if not isinstance(specific, Mapping):
        raise ConfigError(f"[ports.{port}.{name}] must be a table")
    settings.update(specific)
    try:
        cls = import_object(adapters[name])
    except ModuleNotFoundError as error:
        if error.name is None or error.name.partition(".")[0] == "ai_robot":
            raise
        raise ConfigError(
            f"ports.{port}.adapter: {name!r} needs {error.name}, which is not installed "
            "(software/README.md says what each adapter needs)"
        ) from None
    adapter = create(cls, f"ports.{port} ({name})", **settings)
    if not isinstance(adapter, PORT_TYPES[port]):
        raise TypeError(f"{adapters[name]} does not implement {PORT_TYPES[port].__name__}")
    return adapter
