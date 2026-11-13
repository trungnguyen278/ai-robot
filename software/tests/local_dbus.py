# SPDX-License-Identifier: MIT
"""A private D-Bus, for the tests of the adapters that speak to BlueZ and NetworkManager.

The tests need Linux, dbus-fast and the D-Bus daemon (``dbus-daemon``, from Debian's
package of that name). Without them they are skipped. The software workflow installs
them, so they always run in CI.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

START_SECONDS = 10.0  # how long the daemon may take to give its address

CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path={socket}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


@contextmanager
def private_bus(folder: Path) -> Iterator[str]:
    """A D-Bus daemon of its own, with its socket in ``folder``; its address."""
    program = shutil.which("dbus-daemon")
    if sys.platform != "linux" or program is None:
        pytest.skip("needs Linux and dbus-daemon")
    pytest.importorskip("dbus_fast")
    config = folder / "bus.conf"
    config.write_text(CONFIG.format(socket=folder / "bus"), encoding="utf-8")
    daemon = subprocess.Popen(
        [program, f"--config-file={config}", "--nofork", "--print-address"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert daemon.stdout is not None
        address = daemon.stdout.readline().strip()
        if not address:
            raise RuntimeError("dbus-daemon gave no address")
        yield address
    finally:
        daemon.terminate()
        daemon.wait(START_SECONDS)
        if daemon.stdout is not None:
            daemon.stdout.close()


async def impersonate(address: str, signal: Any) -> None:
    """Send ``signal``, a dbus-fast ``Message``, from a connection of no service: what any
    local program may do, on the system bus too, whose policy allows any signal (dbus 1.16,
    bus/system.conf.in). One sent to a single connection reaches it whatever its match rules
    (the D-Bus specification, Message Bus Specification). It returns once the recipient
    has taken it: the bus keeps the order of one connection's messages to another, and the
    recipient answers a ping after it."""
    from dbus_fast import Message
    from dbus_fast.aio import MessageBus

    stranger = await MessageBus(bus_address=address).connect()
    try:
        await stranger.send(signal)
        await stranger.call(
            Message(
                destination=signal.destination,
                path="/",
                interface="org.freedesktop.DBus.Peer",
                member="Ping",
            )
        )
    finally:
        stranger.disconnect()
