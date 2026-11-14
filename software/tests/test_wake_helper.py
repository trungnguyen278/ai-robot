# SPDX-License-Identifier: MIT
"""ai-robot-wake, the helper that presses KEY_WAKEUP, and the robot's client of it
(docs/decisions/0021-sleep-and-wake.md). Linux only: the helper speaks Linux's uinput."""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import struct
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fcntl")

from ai_robot.adapters.wake import helper
from ai_robot.adapters.wake.client import HelperDisplayPower

TIMEOUT = 2.0


class FakeUinput:
    """Stands in for os.open, os.write, os.close and fcntl.ioctl on /dev/uinput."""

    def __init__(self, fail_at: int | None = None) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.fail_at = fail_at  # the request of the ioctl that fails, if any

    def open(self, path: str, flags: int) -> int:
        self.calls.append(("open", path, flags))
        return 42

    def ioctl(self, fd: int, request: int, argument: Any = 0) -> int:
        self.calls.append(("ioctl", fd, request, argument))
        if request == self.fail_at:
            raise OSError("no such device")
        return 0

    def write(self, fd: int, data: bytes) -> int:
        self.calls.append(("write", fd, helper.EVENT.unpack(data)))
        return len(data)

    def close(self, fd: int) -> None:
        self.calls.append(("close", fd))


@pytest.fixture
def uinput(monkeypatch: pytest.MonkeyPatch) -> FakeUinput:
    fake = FakeUinput()
    monkeypatch.setattr(helper.os, "open", fake.open)
    monkeypatch.setattr(helper.os, "write", fake.write)
    monkeypatch.setattr(helper.os, "close", fake.close)
    monkeypatch.setattr(helper.fcntl, "ioctl", fake.ioctl)
    return fake


def test_the_abi_numbers_are_linuxs() -> None:
    # _IOW('U', nr, size) = 1 << 30 | size << 16 | ord('U') << 8 | nr (asm-generic/ioctl.h)
    def iow(nr: int, size: int) -> int:
        return 1 << 30 | size << 16 | ord("U") << 8 | nr

    assert iow(100, struct.calcsize("i")) == helper.UI_SET_EVBIT
    assert iow(101, struct.calcsize("i")) == helper.UI_SET_KEYBIT
    assert helper.SETUP.size == 92  # struct uinput_setup
    assert iow(3, helper.SETUP.size) == helper.UI_DEV_SETUP
    assert (ord("U") << 8 | 1, ord("U") << 8 | 2) == (helper.UI_DEV_CREATE, helper.UI_DEV_DESTROY)
    assert helper.EVENT.size == 24  # struct input_event, with a 64-bit time


def test_the_keyboard_has_the_wake_key_only(uinput: FakeUinput) -> None:
    keyboard = helper.Keyboard("/dev/uinput")
    setup = helper.SETUP.pack(helper.BUS_VIRTUAL, 0, 0, 1, b"ai-robot wake", 0)
    assert uinput.calls == [
        ("open", "/dev/uinput", os.O_WRONLY | os.O_NONBLOCK),
        ("ioctl", 42, helper.UI_SET_EVBIT, helper.EV_KEY),
        ("ioctl", 42, helper.UI_SET_KEYBIT, helper.KEY_WAKEUP),
        ("ioctl", 42, helper.UI_DEV_SETUP, setup),
        ("ioctl", 42, helper.UI_DEV_CREATE, 0),
    ]
    uinput.calls.clear()
    keyboard.press()
    assert uinput.calls == [
        ("write", 42, (0, 0, helper.EV_KEY, 143, 1)),
        ("write", 42, (0, 0, helper.EV_SYN, helper.SYN_REPORT, 0)),
        ("write", 42, (0, 0, helper.EV_KEY, 143, 0)),
        ("write", 42, (0, 0, helper.EV_SYN, helper.SYN_REPORT, 0)),
    ]
    uinput.calls.clear()
    keyboard.close()
    assert uinput.calls == [("ioctl", 42, helper.UI_DEV_DESTROY, 0), ("close", 42)]


def test_a_keyboard_that_cannot_be_made_closes_its_file(uinput: FakeUinput) -> None:
    uinput.fail_at = helper.UI_DEV_CREATE
    with pytest.raises(OSError, match="no such device"):
        helper.Keyboard()
    assert uinput.calls[-1] == ("close", 42)


@contextlib.contextmanager
def serving(tmp_path: Path, clock: list[float] | None = None) -> Iterator[tuple[str, list[str]]]:
    """The helper serving on a socket in ``tmp_path``; what it pressed, and the path."""
    path = str(tmp_path / "wake.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen()
    presses: list[str] = []
    now = clock if clock is not None else [0.0]
    thread = threading.Thread(
        target=helper.serve,
        args=(listener, lambda: presses.append("wake")),
        kwargs={"interval": 1.0, "read_timeout": 0.5, "clock": lambda: now[0]},
    )
    thread.start()
    try:
        yield path, presses
    finally:
        listener.shutdown(socket.SHUT_RDWR)
        listener.close()
        thread.join(TIMEOUT)
        assert not thread.is_alive()


def ask(path: str, request: bytes) -> bytes:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(TIMEOUT)
        connection.connect(path)
        connection.sendall(request)
        connection.shutdown(socket.SHUT_WR)
        return connection.recv(64)


def test_it_presses_at_most_once_per_interval(tmp_path: Path) -> None:
    clock = [100.0]
    with serving(tmp_path, clock) as (path, presses):
        assert ask(path, b"wake\n") == b"ok\n"
        assert presses == ["wake"]
        clock[0] = 100.5
        assert ask(path, b"wake\n") == b"ok\n"  # asked, but too soon
        assert presses == ["wake"]
        clock[0] = 101.0
        assert ask(path, b"wake\n") == b"ok\n"
        assert presses == ["wake", "wake"]


def test_it_does_only_what_it_was_made_for(tmp_path: Path) -> None:
    with serving(tmp_path) as (path, presses):
        for request in (b"key 30\n", b"wake", b"wake\nwake\n", b"w" * 200, b""):
            assert ask(path, request) == b"unknown\n"
        assert presses == []
        assert ask(path, b"wake\n") == b"ok\n"  # still serving
        assert presses == ["wake"]


def test_one_that_says_nothing_does_not_block_the_others(tmp_path: Path) -> None:
    with serving(tmp_path) as (path, presses):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as silent:
            silent.connect(path)  # and sends nothing: the helper gives up on it
            assert ask(path, b"wake\n") == b"ok\n"
        assert presses == ["wake"]


def test_a_press_that_fails_ends_it(tmp_path: Path) -> None:
    # systemd starts it again, with a new keyboard (tools/pi/kiosk/ai-robot-wake.service).
    path = str(tmp_path / "wake.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen()
    errors: list[BaseException] = []

    def broken() -> None:
        raise OSError("no such device")

    def run() -> None:
        try:
            helper.serve(listener, broken, interval=1.0, read_timeout=0.5)
        except OSError as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert ask(path, b"wake\n") == b""  # no answer: the helper is gone
        thread.join(TIMEOUT)
        assert not thread.is_alive()
        assert [str(error) for error in errors] == ["no such device"]
    finally:
        listener.close()
        thread.join(TIMEOUT)


def test_it_needs_its_socket_from_systemd() -> None:
    for environ in ({}, {"LISTEN_PID": "7", "LISTEN_FDS": "1"}, {"LISTEN_PID": "1"}):
        with pytest.raises(SystemExit, match=r"ai-robot-wake\.socket"):
            helper.listening_socket(environ, 1)
    with pytest.raises(SystemExit, match=r"ai-robot-wake\.socket"):
        helper.listening_socket({"LISTEN_PID": "1", "LISTEN_FDS": "2"}, 1)


def test_its_settings_are_required() -> None:
    with pytest.raises(SystemExit):
        helper.main(["--interval", "1", "--settle", "0.5"])  # no --read-timeout


def test_the_client_wakes_the_screen_through_the_helper(tmp_path: Path) -> None:
    async def main() -> None:
        with serving(tmp_path) as (path, presses):
            display = HelperDisplayPower(path=path, timeout=TIMEOUT)
            await display.wake()
            assert presses == ["wake"]

    asyncio.run(main())


def test_the_client_reports_a_helper_that_refuses_or_does_not_answer(tmp_path: Path) -> None:
    path = str(tmp_path / "wake.sock")

    async def main() -> None:
        answers = [b"unknown\n", None]

        async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            assert await reader.readline() == b"wake\n"
            reply = answers.pop(0)
            if reply is None:
                await asyncio.sleep(1.0)  # longer than the client waits
            else:
                writer.write(reply)
            writer.close()

        server = await asyncio.start_unix_server(answer, path)
        async with server:
            display = HelperDisplayPower(path=path, timeout=0.2)
            with pytest.raises(OSError, match="the wake helper refused the request"):
                await display.wake()
            with pytest.raises(OSError, match="the wake helper did not answer in time"):
                await display.wake()
        # The server took its socket away as it closed.
        with pytest.raises(OSError):  # no helper at all
            await display.wake()

    asyncio.run(main())


def test_the_clients_settings_are_checked() -> None:
    with pytest.raises(ValueError, match="needs a path"):
        HelperDisplayPower(path="", timeout=1.0)
    with pytest.raises(ValueError, match="timeout must be a positive number"):
        HelperDisplayPower(path="/run/ai-robot-wake.sock", timeout=0)
