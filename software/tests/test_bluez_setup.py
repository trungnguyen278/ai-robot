# SPDX-License-Identifier: MIT
"""The set-up link through BlueZ, on a private D-Bus with a fake BlueZ (local_dbus.py):
the GATT service and the advertisement it registers, phones that write fragments, and
each phone's channel for indications, which BlueZ takes through ``AcquireNotify``."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import re
from collections.abc import AsyncIterator, Iterator
from functools import partial
from pathlib import Path

import pytest
import waiting
from local_dbus import private_bus

from ai_robot.ports.setup import Connected, Disconnected, LinkEvent, Received

TIMEOUT = 5.0

until = partial(waiting.until, timeout=TIMEOUT)

PHONE = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
OTHER = "/org/bluez/hci0/dev_11_22_33_44_55_66"
HELLO = b'{"type": "hello", "seq": 1, "protocol": "2.0"}'
PROPERTIES = "org.freedesktop.DBus.Properties"
ANSWER = ["org.bluez.GattCharacteristic1", "NotifyAcquired"]


@pytest.fixture
def bus(tmp_path: Path) -> Iterator[str]:
    with private_bus(tmp_path) as address:
        yield address


@contextlib.asynccontextmanager
async def started(
    bus: str, confirm_timeout: float = TIMEOUT, name: str = "Robot"
) -> AsyncIterator[tuple]:
    from fake_bluez import FakeBluez

    from ai_robot.adapters.bluez.setup import BluezSetupLink

    bluez = FakeBluez(bus)
    await bluez.start()
    link = BluezSetupLink(adapter="hci0", bus=bus, confirm_timeout=confirm_timeout)
    await link.open()
    try:
        await link.start(name)
        yield bluez, link
    finally:
        await link.close()
        await bluez.stop()


async def next_event(link: object, timeout: float = TIMEOUT) -> LinkEvent:
    return await asyncio.wait_for(link.event(), timeout)  # type: ignore[attr-defined]


async def connect(bluez: object, link: object, phone: str, mtu: int = 23) -> None:
    """``phone`` enables indications, then says hello, as the protocol has it."""
    from ai_robot.adapters.bluez.setup import REQUEST_UUID

    await bluez.subscribe(phone, mtu)  # type: ignore[attr-defined]
    await bluez.send(phone, REQUEST_UUID, HELLO, mtu)  # type: ignore[attr-defined]
    assert await next_event(link) == Connected(phone)
    assert await next_event(link) == Received(phone, HELLO)


def open_descriptors() -> int:
    return len(os.listdir("/proc/self/fd"))


def test_the_service_and_the_advertisement_are_registered(bus: str) -> None:
    from ai_robot.adapters.bluez.setup import ANSWER_UUID, REQUEST_UUID, SERVICE_UUID

    async def main() -> None:
        async with started(bus, name="Robot with a long name") as (bluez, link):
            services = [
                interfaces["org.bluez.GattService1"]
                for interfaces in bluez.application.values()
                if "org.bluez.GattService1" in interfaces
            ]
            assert services == [{"UUID": SERVICE_UUID, "Primary": True}]
            request = bluez.application[bluez.characteristic(REQUEST_UUID)]
            answer = bluez.application[bluez.characteristic(ANSWER_UUID)]
            assert request["org.bluez.GattCharacteristic1"]["Flags"] == ["write"]
            assert answer["org.bluez.GattCharacteristic1"]["Flags"] == ["indicate"]
            # Its presence has BlueZ hand each phone's indications to a channel.
            assert answer["org.bluez.GattCharacteristic1"]["NotifyAcquired"] is False
            assert "Value" not in answer["org.bluez.GattCharacteristic1"]
            assert bluez.advertisement is not None
            assert bluez.advertisement["ServiceUUIDs"] == [SERVICE_UUID]
            assert bluez.advertisement["LocalName"] == "Robot wi"  # 8 bytes fit
            await link.advertise(False)
            assert bluez.advertisement is None
            await link.advertise(True)
            assert bluez.advertisement is not None
            await link.stop()
            assert bluez.advertisement is None and bluez.application == {}

    asyncio.run(main())


def test_messages_both_ways_in_fragments(bus: str) -> None:
    async def main() -> None:
        async with started(bus) as (bluez, link):
            await connect(bluez, link, PHONE)
            answer = bytes(range(256)) * 3
            await link.send(PHONE, answer)
            assert await bluez.receive(PHONE, TIMEOUT) == answer
            assert bluez.indications[PHONE].empty()
            assert bluez.errors == []  # AcquireNotify got its one reply

    asyncio.run(main(), debug=True)  # which refuses a socket that blocks


# 185 minus the ATT header; past an MTU of 515, the longest value, which BlueZ reads whole.
@pytest.mark.parametrize(("mtu", "longest"), [(185, 182), (517, 512)])
def test_fragments_fit_the_mtu_of_the_phone(bus: str, mtu: int, longest: int) -> None:
    from ai_robot.adapters.bluez.fragments import Joiner

    async def main() -> None:
        async with started(bus) as (bluez, link):
            await connect(bluez, link, PHONE, mtu=mtu)
            await link.send(PHONE, b"y" * 1100)
            joiner, fragments, message = Joiner(), [], None
            while message is None:
                fragments.append(await asyncio.wait_for(bluez.indications[PHONE].get(), TIMEOUT))
                message = joiner.add(fragments[-1])
            assert message == b"y" * 1100
            assert max(len(f) for f in fragments) == longest

    asyncio.run(main())


def test_each_phone_hears_only_what_is_sent_to_it(bus: str) -> None:
    from ai_robot.adapters.bluez.setup import ANSWER_UUID

    async def main() -> None:
        async with started(bus, confirm_timeout=1.0) as (bluez, link):
            await connect(bluez, link, PHONE)
            await connect(bluez, link, OTHER)
            assert bluez.acquired == [True]  # from the first channel on
            (acquired,) = (
                await bluez.call(bluez.characteristic(ANSWER_UUID), PROPERTIES, "Get", "ss", ANSWER)
            ).body
            assert acquired.value is True
            bluez.silent.add(OTHER)
            stuck = asyncio.create_task(link.send(OTHER, b"busy"))
            await link.send(PHONE, b"answer")  # not held up by the phone that is silent
            assert not stuck.done()
            assert await bluez.receive(PHONE, TIMEOUT) == b"answer"
            assert await bluez.receive(OTHER, TIMEOUT) == b"busy"
            assert bluez.indications[PHONE].empty()
            await stuck
            assert await next_event(link) == Disconnected(OTHER)

    asyncio.run(main())


def test_a_phone_that_disconnects_leaves(bus: str) -> None:
    async def main() -> None:
        async with started(bus) as (bluez, link):
            await connect(bluez, link, PHONE)
            bluez.disconnect(PHONE)
            assert await next_event(link) == Disconnected(PHONE)
            await until(lambda: bluez.acquired == [True, False])
            assert not link.acquired
            bluez.disconnect(OTHER)  # one it never knew: nothing
            await link.drop(PHONE)  # gone already: nothing
            assert bluez.disconnected == []

    asyncio.run(main())


def test_the_robot_drops_a_phone(bus: str) -> None:
    async def main() -> None:
        async with started(bus) as (bluez, link):
            await connect(bluez, link, PHONE)
            await link.drop(PHONE)
            assert await next_event(link) == Disconnected(PHONE)
            assert bluez.disconnected == [PHONE]
            assert not link.acquired

    asyncio.run(main())


def test_a_phone_the_robot_drops_loses_its_channel_when_bluez_cannot_disconnect_it(
    bus: str,
) -> None:
    async def main() -> None:
        async with started(bus) as (bluez, link):
            await connect(bluez, link, PHONE)
            bluez.refuse_all = True  # Disconnect fails
            await link.drop(PHONE)
            assert await next_event(link) == Disconnected(PHONE)
            assert not link.acquired
            await until(lambda: bluez.closed == [PHONE])
            bluez.refuse_all = False

    asyncio.run(main())


def test_a_fragment_that_does_not_fit_drops_the_phone(bus: str) -> None:
    from ai_robot.adapters.bluez.setup import REQUEST_UUID

    async def main() -> None:
        async with started(bus) as (bluez, link):
            await bluez.write(PHONE, REQUEST_UUID, bytes([0x40]) + b"no first", mtu=23)
            assert await next_event(link) == Connected(PHONE)
            assert await next_event(link) == Disconnected(PHONE)
            await until(lambda: bluez.disconnected == [PHONE])

    asyncio.run(main())


def test_a_phone_that_does_not_confirm_is_dropped(bus: str) -> None:
    async def main() -> None:
        async with started(bus, confirm_timeout=0.3) as (bluez, link):
            await connect(bluez, link, PHONE)
            bluez.silent.add(PHONE)
            await link.send(PHONE, b"z" * 100)
            assert await next_event(link) == Disconnected(PHONE)
            await until(lambda: bluez.disconnected == [PHONE])

    asyncio.run(main())


def test_a_phone_without_indications_is_dropped_when_the_robot_answers(bus: str) -> None:
    from ai_robot.adapters.bluez.setup import REQUEST_UUID

    async def main() -> None:
        async with started(bus, confirm_timeout=0.3) as (bluez, link):
            await bluez.send(PHONE, REQUEST_UUID, HELLO, mtu=23)  # without enabling them
            await next_event(link)
            await next_event(link)
            await link.send(PHONE, b"answer")
            assert await next_event(link) == Disconnected(PHONE)
            assert bluez.disconnected == [PHONE]

    asyncio.run(main())


def test_a_channel_that_comes_late_is_waited_for(bus: str) -> None:
    from ai_robot.adapters.bluez.setup import REQUEST_UUID

    async def main() -> None:
        async with started(bus) as (bluez, link):
            await bluez.send(PHONE, REQUEST_UUID, HELLO, mtu=23)
            await next_event(link)
            await next_event(link)
            sending = asyncio.create_task(link.send(PHONE, b"answer"))
            await asyncio.sleep(0)  # it runs until it waits for the channel
            await bluez.subscribe(PHONE)  # BlueZ may wait for the MTU exchange first
            await sending
            assert await bluez.receive(PHONE, TIMEOUT) == b"answer"

    asyncio.run(main())


def test_a_phone_that_leaves_while_the_robot_waits_for_its_channel(
    bus: str, caplog: pytest.LogCaptureFixture
) -> None:
    from ai_robot.adapters.bluez.setup import REQUEST_UUID

    async def main() -> None:
        async with started(bus, confirm_timeout=30.0) as (bluez, link):
            await bluez.send(PHONE, REQUEST_UUID, HELLO, mtu=23)
            await next_event(link)
            await next_event(link)
            sending = asyncio.create_task(link.send(PHONE, b"answer"))
            await asyncio.sleep(0)  # it runs until it waits for the channel
            bluez.disconnect(PHONE)
            await asyncio.wait_for(sending, TIMEOUT)  # at once, not after confirm_timeout
            assert await next_event(link) == Disconnected(PHONE)
            assert bluez.disconnected == []  # it left: nothing to drop
            assert "dropped" not in caplog.text

    asyncio.run(main())


def test_a_phone_that_disables_indications_mid_message_is_dropped(bus: str) -> None:
    async def main() -> None:
        async with started(bus, confirm_timeout=30.0) as (bluez, link):
            await connect(bluez, link, PHONE)
            bluez.silent.add(PHONE)
            sending = asyncio.create_task(link.send(PHONE, b"answer"))
            await asyncio.wait_for(bluez.indications[PHONE].get(), TIMEOUT)
            bluez.unsubscribe(PHONE)
            await asyncio.wait_for(sending, TIMEOUT)  # at once, not after confirm_timeout
            assert await next_event(link) == Disconnected(PHONE)
            assert bluez.disconnected == [PHONE]

    asyncio.run(main())


def test_a_phone_that_enables_indications_again_gets_a_new_channel(bus: str) -> None:
    async def main() -> None:
        async with started(bus) as (bluez, link):
            await connect(bluez, link, PHONE)
            bluez.unsubscribe(PHONE)
            await bluez.subscribe(PHONE)
            await link.send(PHONE, b"answer")
            assert await bluez.receive(PHONE, TIMEOUT) == b"answer"
            assert link.acquired

    asyncio.run(main())


def test_a_channel_bluez_shut_down_drops_the_phone_at_once(bus: str) -> None:
    async def main() -> None:
        async with started(bus, confirm_timeout=30.0) as (bluez, link):
            await connect(bluez, link, PHONE)
            bluez.unsubscribe(PHONE)  # before the robot has read that it closed
            await asyncio.wait_for(link.send(PHONE, b"answer"), TIMEOUT)
            assert await next_event(link) == Disconnected(PHONE)

    asyncio.run(main())


def test_a_second_channel_for_a_phone_closes_the_first(bus: str) -> None:
    async def main() -> None:
        async with started(bus) as (bluez, link):
            await connect(bluez, link, PHONE)
            await bluez.subscribe(PHONE, again=True)
            await until(lambda: bluez.closed == [PHONE])
            await link.send(PHONE, b"answer")
            assert await bluez.receive(PHONE, TIMEOUT) == b"answer"

    asyncio.run(main())


def test_a_link_closed_without_bluez_closes_its_channels(bus: str) -> None:
    from fake_bluez import FakeBluez

    from ai_robot.adapters.bluez.setup import BluezSetupLink

    async def main() -> None:
        bluez = FakeBluez(bus)
        await bluez.start()
        link = BluezSetupLink(adapter="hci0", bus=bus, confirm_timeout=TIMEOUT)
        await link.open()
        try:
            await link.start("Robot")
            await bluez.subscribe(PHONE)
            bluez.refuse_all = True  # BlueZ fails every call from now on
            await link.close()
            assert not link.acquired
            await until(lambda: bluez.closed == [PHONE])
        finally:
            await bluez.stop()

    asyncio.run(main())


def test_no_channel_to_give_is_a_failure(bus: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import ai_robot.adapters.bluez.setup as adapter

    def no_socket(*_: object) -> None:
        raise OSError(errno.EMFILE, os.strerror(errno.EMFILE))

    async def main() -> None:
        async with started(bus) as (bluez, link):
            monkeypatch.setattr(adapter.socket, "socketpair", no_socket)
            with pytest.raises(ConnectionError, match=re.escape("org.bluez.Error.Failed")):
                await bluez.subscribe(PHONE)
            assert not link.acquired

    asyncio.run(main())


def test_channels_are_closed_and_none_is_left_open(bus: str) -> None:
    async def main() -> None:
        async with started(bus) as (bluez, link):
            before = open_descriptors()
            await bluez.subscribe(PHONE)  # enabled, and never wrote
            await bluez.subscribe(OTHER)
            await until(lambda: open_descriptors() == before + 4)  # the robot's end and BlueZ's
            await link.stop()
            await until(lambda: sorted(bluez.closed) == sorted([PHONE, OTHER]))
            assert not link.acquired
            await until(lambda: open_descriptors() == before)

    asyncio.run(main())


def test_a_channel_for_no_known_device_is_refused(bus: str) -> None:
    from ai_robot.adapters.bluez.setup import ANSWER_UUID

    async def main() -> None:
        async with started(bus) as (bluez, link):
            # Failed, so that BlueZ does not fall back on StartNotify.
            failed = re.escape("org.bluez.Error.Failed")
            with pytest.raises(ConnectionError, match=failed):
                await bluez.subscribe("/org/bluez/hci1/dev_AA_BB_CC_DD_EE_FF")
            answer = bluez.characteristic(ANSWER_UUID)
            for signature, body in (("", []), ("s", ["device"])):  # options that are not a{sv}
                with pytest.raises(ConnectionError, match=failed):
                    await bluez.call(answer, ANSWER[0], "AcquireNotify", signature, body)
            assert not link.acquired

    asyncio.run(main())


def test_a_refusal_from_bluez_is_an_error(bus: str) -> None:
    from fake_bluez import FakeBluez

    from ai_robot.adapters.bluez.setup import BluezSetupLink

    async def main() -> None:
        bluez = FakeBluez(bus)
        await bluez.start()
        link = BluezSetupLink(adapter="hci0", bus=bus, confirm_timeout=TIMEOUT)
        await link.open()
        try:
            bluez.refuse = True
            with pytest.raises(ConnectionError, match="RegisterApplication failed"):
                await link.start("Robot")
        finally:
            await link.close()
            await bluez.stop()

    asyncio.run(main())


def test_settings_are_checked() -> None:
    pytest.importorskip("dbus_fast")
    from ai_robot.adapters.bluez.setup import BluezSetupLink, short_name

    with pytest.raises(ValueError, match="adapter"):
        BluezSetupLink(adapter="", bus="system", confirm_timeout=1.0)
    with pytest.raises(ValueError, match="confirm_timeout"):
        BluezSetupLink(adapter="hci0", bus="system", confirm_timeout=0)
    assert short_name("Rôbôtô") == "Rôbôt"  # cut on a whole character, within 8 bytes
