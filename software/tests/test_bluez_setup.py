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
from local_dbus import impersonate, private_bus

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


# ---- BlueZ that stops, starts, and loses its controller ----


def test_bluez_that_restarts_ends_its_apps_and_takes_the_service_again(bus: str) -> None:
    async def main() -> None:
        async with started(bus) as (bluez, link):
            await connect(bluez, link, PHONE)
            bluez.registrations.clear()
            await bluez.stop()  # as installing a bluez package does
            assert await next_event(link) == Disconnected(PHONE)
            assert not link.acquired
            await bluez.start()  # a new connection, which knows nothing of before
            await until(lambda: bluez.advertisement is not None)
            assert bluez.advertisement["LocalName"] == "Robot"
            assert bluez.registrations == ["application", "advertisement"]
            await connect(bluez, link, PHONE)  # set-up goes on
            await link.send(PHONE, b"info")
            assert await bluez.receive(PHONE, TIMEOUT) == b"info"

    asyncio.run(main(), debug=True)


def test_what_the_robot_asks_while_bluez_is_away_holds_once_it_is_back(bus: str) -> None:
    async def main() -> None:
        async with started(bus) as (bluez, link):
            await bluez.stop()
            await until(lambda: link._bluez.unique is None)  # the link has heard it
            await link.advertise(False)  # no error: there is nothing to unregister
            bluez.registrations.clear()
            await bluez.start()
            await until(lambda: bluez.application)
            await link.advertise(False)  # once the link has registered again
            assert bluez.registrations == ["application"]
            await link.advertise(True)
            assert bluez.registrations == ["application", "advertisement"]
            await bluez.stop()
            await until(lambda: link._bluez.unique is None)
            await link.stop()
            await bluez.start()
            await link.advertise(True)  # not offered: nothing
            assert bluez.registrations == ["application", "advertisement"]

    asyncio.run(main())


def test_a_controller_that_goes_away_and_comes_back(
    bus: str, caplog: pytest.LogCaptureFixture
) -> None:
    from fake_bluez import ADVERTISING_MANAGER, GATT_MANAGER, MANAGERS

    async def main() -> None:
        async with started(bus) as (bluez, link):
            await connect(bluez, link, PHONE)
            bluez.registrations.clear()
            bluez.remove(*MANAGERS)
            assert await next_event(link) == Disconnected(PHONE)
            bluez.add("org.bluez.Adapter1", GATT_MANAGER)
            await until(lambda: bluez.application)
            bluez.add(ADVERTISING_MANAGER)  # once the controller told its features
            await until(lambda: bluez.advertisement is not None)
            assert bluez.registrations == ["application", "advertisement"]
            bluez.remove(ADVERTISING_MANAGER)
            bluez.add(ADVERTISING_MANAGER)
            await until(lambda: len(bluez.registrations) == 3)
            assert bluez.registrations[2] == "advertisement"
            bluez.remove(*MANAGERS)
            bluez.add("org.bluez.Adapter1", GATT_MANAGER)
            bluez.add(ADVERTISING_MANAGER)  # at once: two restorations meet
            await until(lambda: len(bluez.registrations) == 5)
            assert bluez.registrations[3:] == ["application", "advertisement"]
        assert "cannot register" not in caplog.text

    asyncio.run(main())


def test_another_controller_that_goes_changes_nothing(bus: str) -> None:
    from dbus_fast import Message
    from fake_bluez import MANAGERS, OBJECT_MANAGER

    async def main() -> None:
        async with started(bus) as (bluez, link):
            bluez.registrations.clear()
            removed = ["/org/bluez/hci1", list(MANAGERS)]
            bluez.bus.send(
                Message.new_signal("/", OBJECT_MANAGER, "InterfacesRemoved", "oas", removed)
            )
            ping = Message(
                destination=link._bus.unique_name,
                path="/",
                interface="org.freedesktop.DBus.Peer",
                member="Ping",
            )
            await bluez.bus.call(ping)  # the link has taken the signal, sent before
            await link.advertise(False)
            assert bluez.registrations == ["-advertisement"]

    asyncio.run(main())


def test_starting_needs_bluez_and_its_managers(bus: str) -> None:
    from fake_bluez import ADVERTISING_MANAGER, GATT_MANAGER, FakeBluez

    from ai_robot.adapters.bluez.setup import BluezSetupLink

    async def start(link: object) -> None:
        """Start ``link`` once it has heard that BlueZ offers what it needs."""
        async with asyncio.timeout(TIMEOUT):
            while True:
                with contextlib.suppress(ConnectionError):
                    return await link.start("Robot")  # type: ignore[attr-defined]
                await asyncio.sleep(0.01)

    async def main() -> None:
        link = BluezSetupLink(adapter="hci0", bus=bus, confirm_timeout=TIMEOUT)
        await link.open()  # before BlueZ runs
        bluez = FakeBluez(bus)
        try:
            with pytest.raises(ConnectionError, match="no GATT service and advertising"):
                await link.start("Robot")
            await bluez.start(offers=("org.bluez.Adapter1", GATT_MANAGER))
            await asyncio.sleep(0.1)
            with pytest.raises(ConnectionError, match="no GATT service and advertising"):
                await link.start("Robot")
            assert bluez.registrations == []
            bluez.add(ADVERTISING_MANAGER)
            await start(link)
            assert bluez.registrations == ["application", "advertisement"]
        finally:
            await link.close()
            await bluez.stop()

    asyncio.run(main())


def test_a_start_that_fails_leaves_nothing_registered(bus: str) -> None:
    async def main() -> None:
        async with started(bus) as (bluez, link):
            await link.stop()
            bluez.registrations.clear()
            bluez.refuse_advertisement = True
            with pytest.raises(ConnectionError, match="RegisterAdvertisement failed"):
                await link.start("Robot")
            assert bluez.registrations == ["application", "-application"]
            await bluez.stop()
            await bluez.start()  # BlueZ that restarts: nothing to register again
            await link.advertise(True)
            assert bluez.registrations == ["application", "-application"]

    asyncio.run(main())


def test_a_listing_older_than_a_change_is_taken_again(bus: str) -> None:
    from fake_bluez import ADVERTISING_MANAGER, GATT_MANAGER, FakeBluez

    from ai_robot.adapters.bluez.setup import BluezSetupLink

    async def main() -> None:
        bluez = FakeBluez(bus)
        await bluez.start(offers=("org.bluez.Adapter1", GATT_MANAGER))
        bluez.late = {ADVERTISING_MANAGER}  # comes as BlueZ lists, after its listing
        link = BluezSetupLink(adapter="hci0", bus=bus, confirm_timeout=TIMEOUT)
        await link.open()
        try:
            await link.start("Robot")
            assert bluez.advertisement is not None
        finally:
            await link.close()
            await bluez.stop()

    asyncio.run(main())


def test_a_listing_from_a_bluez_that_stopped_is_not_taken(bus: str) -> None:
    from dbus_fast import Message
    from fake_bluez import ADAPTER, BLUEZ, FakeBluez

    from ai_robot.adapters.bluez.setup import BluezSetupLink

    async def main() -> None:
        bluez = FakeBluez(bus)
        await bluez.start()
        bluez.hold = True
        link = BluezSetupLink(adapter="hci0", bus=bus, confirm_timeout=TIMEOUT)
        opening = asyncio.create_task(link.open())
        try:
            await until(lambda: bluez.held)
            await bluez.bus.release_name(BLUEZ)  # BlueZ stops as it answers
            objects = {ADAPTER: {name: {} for name in bluez.managers}}
            bluez.bus.send(Message.new_method_return(bluez.held[0], "a{oa{sa{sv}}}", [objects]))
            await opening
            with pytest.raises(ConnectionError, match="offers no"):
                await link.start("Robot")
        finally:
            await link.close()
            await bluez.stop()

    asyncio.run(main())


def test_a_registration_bluez_loses_as_it_answers_is_made_again(bus: str) -> None:
    from fake_bluez import MANAGERS

    async def main() -> None:
        async with started(bus) as (bluez, link):
            await link.stop()
            bluez.registrations.clear()
            bluez.vanish = MANAGERS  # the controller goes away as BlueZ registers
            await link.start("Robot")
            assert bluez.application == {} and bluez.advertisement is None
            bluez.add(*MANAGERS)
            await until(lambda: bluez.advertisement is not None)
            assert bluez.registrations == ["application", "advertisement"]

    asyncio.run(main())


def test_an_advertisement_bluez_loses_as_it_answers_is_made_again(bus: str) -> None:
    from fake_bluez import ADVERTISING_MANAGER

    async def main() -> None:
        async with started(bus) as (bluez, link):
            await link.advertise(False)
            bluez.registrations.clear()
            bluez.vanish = (ADVERTISING_MANAGER,)
            await link.advertise(True)
            assert bluez.advertisement is None
            bluez.add(ADVERTISING_MANAGER)
            await until(lambda: bluez.advertisement is not None)
            assert bluez.registrations == ["advertisement"]

    asyncio.run(main())


def test_another_interface_that_goes_takes_no_registration(bus: str) -> None:
    async def main() -> None:
        async with started(bus) as (bluez, link):
            await link.stop()
            bluez.registrations.clear()
            bluez.vanish = ("org.bluez.Media1",)  # as BlueZ registers the service
            await link.start("Robot")
            assert bluez.registrations == ["application", "advertisement"]

    asyncio.run(main())


def test_closing_ends_what_bluez_started(bus: str) -> None:
    from fake_bluez import FakeBluez

    from ai_robot.adapters.bluez.setup import BluezSetupLink

    async def main() -> None:
        bluez = FakeBluez(bus)
        await bluez.start()
        link = BluezSetupLink(adapter="hci0", bus=bus, confirm_timeout=TIMEOUT)
        await link.open()
        await link.start("Robot")
        await bluez.stop()
        bluez.hold = True  # BlueZ does not answer the link's listing
        await bluez.start(offers=())
        await until(lambda: bluez.held)
        await asyncio.wait_for(link.close(), TIMEOUT)
        assert asyncio.all_tasks() == {asyncio.current_task()}
        await bluez.stop()

    asyncio.run(main())


# ---- only BlueZ, and the bus, are heard ----


def test_only_bluez_and_the_bus_are_heard(bus: str) -> None:
    """Another program's signals, sent to the robot alone, change nothing."""
    from dbus_fast import Message, Variant
    from fake_bluez import MANAGERS

    async def main() -> None:
        async with started(bus) as (bluez, link):
            await connect(bluez, link, PHONE)
            bluez.registrations.clear()
            signals = [
                Message.new_signal(
                    PHONE,
                    PROPERTIES,
                    "PropertiesChanged",
                    "sa{sv}as",
                    ["org.bluez.Device1", {"Connected": Variant("b", False)}, []],
                ),
                Message.new_signal(
                    "/",
                    "org.freedesktop.DBus.ObjectManager",
                    "InterfacesRemoved",
                    "oas",
                    ["/org/bluez/hci0", list(MANAGERS)],
                ),
                Message.new_signal(
                    "/org/freedesktop/DBus",
                    "org.freedesktop.DBus",
                    "NameOwnerChanged",
                    "sss",
                    ["org.bluez", bluez.bus.unique_name, ""],
                ),
            ]
            for signal in signals:
                signal.destination = link._bus.unique_name
                await impersonate(bus, signal)
            with pytest.raises(TimeoutError):
                await next_event(link, 0.1)
            assert link.acquired
            await link.send(PHONE, b"info")
            assert await bluez.receive(PHONE, TIMEOUT) == b"info"
            await link.advertise(False)
            assert bluez.registrations == ["-advertisement"]  # it was still registered

    asyncio.run(main())


def test_settings_are_checked() -> None:
    pytest.importorskip("dbus_fast")
    from ai_robot.adapters.bluez.setup import BluezSetupLink, short_name

    with pytest.raises(ValueError, match="adapter"):
        BluezSetupLink(adapter="", bus="system", confirm_timeout=1.0)
    with pytest.raises(ValueError, match="confirm_timeout"):
        BluezSetupLink(adapter="hci0", bus="system", confirm_timeout=0)
    assert short_name("Rôbôtô") == "Rôbôt"  # cut on a whole character, within 8 bytes
