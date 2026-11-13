# SPDX-License-Identifier: MIT
"""The robot's Wi-Fi through NetworkManager, on a private D-Bus with a fake NetworkManager
(local_dbus.py): scans, joins that succeed and fail, and the status."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import waiting
from local_dbus import impersonate, private_bus

from ai_robot.ports.network import JoinError, JoinFailure, NetworkState, Security

PSK, SAE, OWE, EAP = 0x100, 0x400, 0x800, 0x200


@pytest.fixture
def bus(tmp_path: Path) -> Iterator[str]:
    with private_bus(tmp_path) as address:
        yield address


def points() -> list[Any]:
    from fake_networkmanager import AccessPoint

    return [
        AccessPoint(b"Home", 80, flags=1, rsn=PSK, password="correct horse"),
        AccessPoint(b"Home", 50, flags=1, rsn=PSK, password="correct horse"),  # another one
        AccessPoint(b"Modern", 60, flags=1, rsn=SAE, password="sae password"),
        AccessPoint(b"Mixed", 55, flags=1, rsn=PSK | SAE, password="mixed password"),
        AccessPoint(b"Caf\xc3\xa9", 40),
        AccessPoint(b"Enhanced", 35, rsn=OWE),
        AccessPoint(b"Office", 30, flags=1, rsn=EAP),
        AccessPoint(b"Old", 20, flags=1),  # WEP
        AccessPoint(b"Secret", 70, flags=1, rsn=PSK, password="hidden one", hidden=True),
        AccessPoint(b"\xff\x00Odd", 10),
        AccessPoint(b"NoDhcp", 15, password=None, dhcp=False),
    ]


@contextlib.asynccontextmanager
async def started(bus: str, scan_timeout: float = 1.0) -> AsyncIterator[tuple[Any, Any]]:
    from fake_networkmanager import FakeNetworkManager

    from ai_robot.adapters.networkmanager.network import NetworkManagerNetwork

    manager = FakeNetworkManager(bus, points())
    await manager.start()
    network = NetworkManagerNetwork(interface="wlan0", bus=bus, scan_timeout=scan_timeout)
    await network.open()
    try:
        yield manager, network
    finally:
        await network.close()
        await manager.stop()


def test_a_scan_lists_each_network_once_with_its_security(bus: str) -> None:
    async def main() -> None:
        async with started(bus) as (manager, network):
            manager.save(b"Modern", "sae password")
            found = await network.scan()
            assert "RequestScan" in manager.calls
            assert [(n.ssid, n.security, n.signal, n.saved) for n in found] == [
                (b"Home", Security.WPA2, 80, False),
                (b"Modern", Security.WPA3, 60, True),
                (b"Mixed", Security.WPA2, 55, False),
                ("Café".encode(), Security.OPEN, 40, False),
                (b"Enhanced", Security.OPEN, 35, False),
                (b"Office", Security.OTHER, 30, False),
                (b"Old", Security.OTHER, 20, False),
                (b"NoDhcp", Security.OPEN, 15, False),
                (b"\xff\x00Odd", Security.OPEN, 10, False),
            ]

    asyncio.run(main())


def test_joining_with_the_right_password(bus: str) -> None:
    async def main() -> None:
        async with started(bus) as (manager, network):
            assert (await network.status()).state is NetworkState.DISCONNECTED
            status = await network.join(
                b"Home", "correct horse", hidden=False, security=None, timeout=5.0
            )
            assert (status.state, status.ssid, status.address, status.internet) == (
                NetworkState.CONNECTED,
                b"Home",
                "192.168.1.23",
                True,
            )
            (profile,) = manager.profiles.values()
            assert profile["802-11-wireless"]["ssid"] == b"Home"
            assert profile["802-11-wireless-security"] == {"psk": "correct horse"}
            assert profile["connection"]["autoconnect"] is True

    asyncio.run(main())


@pytest.mark.parametrize(
    ("ssid", "password", "failure"),
    [
        (b"Home", "wrong horse", JoinFailure.WRONG_PASSWORD),
        (b"Nowhere", "anything", JoinFailure.NOT_FOUND),
        (b"NoDhcp", None, JoinFailure.NO_ADDRESS),
    ],
)
def test_a_failed_join_leaves_no_profile(
    bus: str, ssid: bytes, password: str | None, failure: JoinFailure
) -> None:
    async def main() -> None:
        async with started(bus) as (manager, network):
            with pytest.raises(JoinError) as error:
                await network.join(ssid, password, hidden=False, security=None, timeout=5.0)
            assert error.value.failure is failure
            assert manager.profiles == {}

    asyncio.run(main())


def test_joining_a_hidden_network(bus: str) -> None:
    async def main() -> None:
        async with started(bus) as (manager, network):
            status = await network.join(
                b"Secret", "hidden one", hidden=True, security=Security.WPA2, timeout=5.0
            )
            assert status.state is NetworkState.CONNECTED
            (profile,) = manager.profiles.values()
            assert profile["802-11-wireless"]["hidden"] is True
            assert profile["802-11-wireless-security"]["key-mgmt"] == "wpa-psk"

    asyncio.run(main())


def test_joining_a_saved_network_needs_no_password(bus: str) -> None:
    async def main() -> None:
        async with started(bus) as (manager, network):
            saved = manager.save(b"Home", "correct horse")
            status = await network.join(b"Home", None, hidden=False, security=None, timeout=5.0)
            assert status.state is NetworkState.CONNECTED
            assert "ActivateConnection" in manager.calls
            assert list(manager.profiles) == [saved]

    asyncio.run(main())


def test_a_new_password_replaces_the_saved_one_once_it_works(bus: str) -> None:
    async def main() -> None:
        async with started(bus) as (manager, network):
            old = manager.save(b"Home", "an old password")
            with pytest.raises(JoinError):
                await network.join(b"Home", "wrong horse", hidden=False, security=None, timeout=5.0)
            assert list(manager.profiles) == [old]  # a wrong one loses nothing
            await network.join(b"Home", "correct horse", hidden=False, security=None, timeout=5.0)
            assert old not in manager.profiles and len(manager.profiles) == 1

    asyncio.run(main())


def test_a_join_that_takes_too_long_is_abandoned(bus: str) -> None:
    async def main() -> None:
        async with started(bus) as (manager, network):
            manager.stall = True
            with pytest.raises(JoinError) as error:
                await network.join(
                    b"Home", "correct horse", hidden=False, security=None, timeout=0.3
                )
            assert error.value.failure is JoinFailure.OTHER
            assert "DeactivateConnection" in manager.calls and manager.profiles == {}

    asyncio.run(main())


def test_only_networkmanager_tells_the_state_of_the_device(bus: str) -> None:
    """Another program's signal, sent to the robot alone, does not end a join."""
    from dbus_fast import Message
    from fake_networkmanager import CONFIG, DEVICE_PATH, FAILED, NEED_AUTH, NM, NO_SECRETS

    async def main() -> None:
        async with started(bus) as (manager, network):
            manager.stall = True
            joining = asyncio.create_task(
                network.join(b"Home", "correct horse", hidden=False, security=None, timeout=1.0)
            )
            await waiting.until(
                lambda: manager.state == CONFIG, timeout=5.0
            )  # the join waits for news
            for state in (NEED_AUTH, FAILED):
                signal = Message.new_signal(
                    DEVICE_PATH, f"{NM}.Device", "StateChanged", "uuu", [state, 0, NO_SECRETS]
                )
                signal.destination = network._bus.unique_name
                await impersonate(bus, signal)
            with pytest.raises(JoinError) as error:
                await joining
            assert error.value.failure is JoinFailure.OTHER  # it took too long

    asyncio.run(main())


def test_networkmanager_is_heard_after_it_restarts(bus: str) -> None:
    async def main() -> None:
        async with started(bus) as (manager, network):
            await manager.stop()
            await manager.start()  # on a new connection, with a new unique name
            status = await network.join(
                b"Home", "correct horse", hidden=False, security=None, timeout=5.0
            )
            assert status.state is NetworkState.CONNECTED

    asyncio.run(main())


def test_an_unknown_interface_is_an_error(bus: str) -> None:
    from fake_networkmanager import FakeNetworkManager

    from ai_robot.adapters.networkmanager.network import NetworkManagerNetwork

    async def main() -> None:
        manager = FakeNetworkManager(bus, [])
        await manager.start()
        try:
            network = NetworkManagerNetwork(interface="wlan9", bus=bus, scan_timeout=1.0)
            with pytest.raises(ConnectionError, match="GetDeviceByIpIface"):
                await network.open()
        finally:
            await manager.stop()

    asyncio.run(main())


def test_settings_are_checked() -> None:
    pytest.importorskip("dbus_fast")
    from ai_robot.adapters.networkmanager.network import NetworkManagerNetwork

    with pytest.raises(ValueError, match="interface"):
        NetworkManagerNetwork(interface="", bus="system", scan_timeout=1.0)
    with pytest.raises(ValueError, match="scan_timeout"):
        NetworkManagerNetwork(interface="wlan0", bus="system", scan_timeout=0)
