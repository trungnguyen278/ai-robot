# SPDX-License-Identifier: MIT
"""ai-robot-wake: presses ``KEY_WAKEUP`` on a keyboard that has no other key, when the
robot asks, and when a touch lands on a dark screen (docs/decisions/0021-sleep-and-wake.md).

A dark screen lights up on input, and the robot has no other way to light it. The robot
gets no uinput access of its own: this helper creates a uinput keyboard with that one
key, and presses it at most once per ``--interval`` seconds, whoever asks how often.

Mir passes on no touch from a screen that is off, so a touch alone leaves it dark. The
helper therefore reads the touch screens as well, beside the compositor, and presses the
key when a touch begins while no display is on.

It runs as its own service, with only the ``uinput`` group and the ``touchscreen`` group,
which may read the touch screens and nothing else, on a socket that systemd opens and that
only the robot's group may use (tools/pi/kiosk/). It uses the standard library only, so
that tools/pi/kiosk.sh can install it on its own.

Each connection sends ``REQUEST`` and receives ``ANSWER_OK``, or ``ANSWER_UNKNOWN`` for
anything else.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import functools
import math
import os
import selectors
import socket
import struct
import sys
import time
from collections.abc import Callable

REQUEST = b"wake\n"
ANSWER_OK = b"ok\n"
ANSWER_UNKNOWN = b"unknown\n"
REQUEST_LIMIT = 64  # bytes read from a connection, at most

# Linux's uinput ABI: include/uapi/linux/uinput.h, input.h and input-event-codes.h, and
# the "keyboard events" example of Documentation/input/uinput.rst.
UINPUT = "/dev/uinput"
UI_SET_EVBIT = 0x40045564  # _IOW('U', 100, int)
UI_SET_KEYBIT = 0x40045565  # _IOW('U', 101, int)
UI_DEV_SETUP = 0x405C5503  # _IOW('U', 3, struct uinput_setup): 92 bytes
UI_DEV_CREATE = 0x5501  # _IO('U', 1)
UI_DEV_DESTROY = 0x5502  # _IO('U', 2)
EV_SYN = 0x00
EV_KEY = 0x01
SYN_REPORT = 0
KEY_WAKEUP = 143
BUS_VIRTUAL = 0x06
DEVICE_NAME = b"ai-robot wake"
SETUP = struct.Struct("HHHH80sI")  # struct uinput_setup: input_id, name, ff_effects_max
EVENT = struct.Struct("llHHi")  # struct input_event: a timeval, then type, code, value

# Touch screens: Linux's evdev ABI. A touch begins with BTN_TOUCH at 1, which the kernel
# reports for every direct touch device (drivers/input/input-mt.c,
# input_mt_report_pointer_emulation). A read returns whole events only, and fails with
# ENODEV once the device has gone (drivers/input/evdev.c, evdev_read).
BTN_TOUCH = 0x14A
DEVICE_PREFIX = "event"  # the event devices' names, in /dev/input
EVENTS_READ = 64  # events read from a touch screen at a time, at most

# The kernel's display connectors (drivers/gpu/drm/drm_sysfs.c): each has a `status`,
# "connected", "disconnected" or "unknown", and a `dpms`, "On", "Standby", "Suspend" or
# "Off".
CONNECTED = "connected"
POWER_ON = "On"

# sd_listen_fds(3): the first socket systemd passes.
SD_LISTEN_FDS_START = 3


class Keyboard:
    """A uinput keyboard with ``KEY_WAKEUP`` only."""

    def __init__(self, path: str = UINPUT) -> None:
        self.fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        try:
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
            fcntl.ioctl(self.fd, UI_SET_KEYBIT, KEY_WAKEUP)
            fcntl.ioctl(self.fd, UI_DEV_SETUP, SETUP.pack(BUS_VIRTUAL, 0, 0, 1, DEVICE_NAME, 0))
            fcntl.ioctl(self.fd, UI_DEV_CREATE)
        except OSError:
            os.close(self.fd)
            raise

    def press(self) -> None:
        """Press the key and release it."""
        for kind, code, value in (
            (EV_KEY, KEY_WAKEUP, 1),
            (EV_SYN, SYN_REPORT, 0),
            (EV_KEY, KEY_WAKEUP, 0),
            (EV_SYN, SYN_REPORT, 0),
        ):
            os.write(self.fd, EVENT.pack(0, 0, kind, code, value))

    def close(self) -> None:
        try:
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        finally:
            os.close(self.fd)


class Touchscreens:
    """The touch screens among the event devices of ``folder`` (``/dev/input``) that this
    helper may read: udev lets its group read those and no other (tools/pi/kiosk/). It
    reads them beside the compositor, without grabbing them. One that goes, as when USB
    enumerates it again, is forgotten, and ``scan`` opens it again once it is back: the
    helper looks every ``rescan`` seconds."""

    def __init__(self, folder: str, rescan: float) -> None:
        self.folder = folder
        self.rescan = rescan
        self.files: dict[str, int] = {}  # each open device's path, and its file

    def scan(self) -> list[tuple[str, int]]:
        """Open the devices not open yet that this helper may read; their paths and files."""
        try:
            names = sorted(os.listdir(self.folder))
        except OSError:  # none for now
            return []
        opened = []
        for name in names:
            path = os.path.join(self.folder, name)
            if not name.startswith(DEVICE_PREFIX) or path in self.files:
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)  # not inherited (PEP 446)
            except OSError:  # not one this helper may read
                continue
            self.files[path] = fd
            opened.append((path, fd))
        return opened

    def read(self, fd: int) -> bytes:
        """The events ``fd`` has to read; none once its device has gone."""
        try:
            return os.read(fd, EVENTS_READ * EVENT.size)
        except OSError:
            return b""

    def forget(self, path: str) -> None:
        os.close(self.files.pop(path))

    def close(self) -> None:
        for path in list(self.files):
            self.forget(path)


def began(data: bytes) -> bool:
    """Whether ``data``, whole events read from a touch screen, holds a touch beginning."""
    return any(
        kind == EV_KEY and code == BTN_TOUCH and value == 1
        for _, _, kind, code, value in EVENT.iter_unpack(data)
    )


def dark(folder: str) -> bool:
    """Whether no display is on: no connector of ``folder`` (``/sys/class/drm``) both
    ``connected`` and ``On``. A folder that cannot be read counts as dark: a key pressed
    for nothing costs nothing, and one not pressed leaves the screen dark."""
    try:
        names = os.listdir(folder)
    except OSError:
        return True
    for name in names:
        try:
            if _attribute(folder, name, "status") == CONNECTED and (
                _attribute(folder, name, "dpms") == POWER_ON
            ):
                return False
        except OSError:  # not a connector
            continue
    return True


def _attribute(folder: str, name: str, attribute: str) -> str:
    with open(os.path.join(folder, name, attribute), encoding="ascii") as file:
        return file.read().strip()


def read_request(connection: socket.socket) -> bytes:
    """What a connection sends, up to its first newline and ``REQUEST_LIMIT`` bytes."""
    data = b""
    while not data.endswith(b"\n") and len(data) < REQUEST_LIMIT:
        chunk = connection.recv(REQUEST_LIMIT - len(data))
        if not chunk:
            break
        data += chunk
    return data


def serve(
    listener: socket.socket,
    press: Callable[[], None],
    *,
    interval: float,
    read_timeout: float,
    dark: Callable[[], bool],
    touchscreens: Touchscreens | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Answer the connections to ``listener`` one at a time, and watch ``touchscreens``
    for a touch beginning while the screen is ``dark()``. Press for either, at most once
    per ``interval`` seconds, until the listener is shut down.

    A press that fails ends it: systemd starts the helper again, with a new keyboard."""
    last = -math.inf

    def wake() -> None:
        nonlocal last
        now = clock()
        if now - last >= interval:
            press()
            last = now

    scanned = -math.inf
    with selectors.DefaultSelector() as selector:
        selector.register(listener, selectors.EVENT_READ)
        while True:
            if touchscreens is not None and clock() - scanned >= touchscreens.rescan:
                for path, fd in touchscreens.scan():
                    selector.register(fd, selectors.EVENT_READ, path)
                scanned = clock()
            timeout = None if touchscreens is None else touchscreens.rescan
            for key, _ in selector.select(timeout):
                if key.fileobj is listener:
                    if not answer(listener, wake, read_timeout):
                        return
                    continue
                assert touchscreens is not None
                data = touchscreens.read(key.fd)
                if not data:  # it went
                    selector.unregister(key.fd)
                    touchscreens.forget(key.data)
                elif began(data) and dark():
                    wake()


def answer(listener: socket.socket, wake: Callable[[], None], read_timeout: float) -> bool:
    """Answer the next connection to ``listener``. ``False`` once it is shut down."""
    try:
        connection, _ = listener.accept()
    except OSError:  # shut down
        return False
    with connection:
        connection.settimeout(read_timeout)
        try:
            request = read_request(connection)
        except OSError:  # it left, or was too slow: the next one
            return True
        reply = ANSWER_UNKNOWN
        if request == REQUEST:
            wake()
            reply = ANSWER_OK
        with contextlib.suppress(OSError):  # it left before the answer
            connection.sendall(reply)
    return True


def listening_socket(environ: dict[str, str], pid: int) -> socket.socket:
    """The one socket systemd passed (sd_listen_fds(3)). ``SystemExit`` without it."""
    if environ.get("LISTEN_PID") != str(pid) or environ.get("LISTEN_FDS") != "1":
        raise SystemExit("ai-robot-wake needs one socket from systemd (ai-robot-wake.socket)")
    return socket.socket(fileno=SD_LISTEN_FDS_START)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ai-robot-wake", description=__doc__.split("\n")[0])
    parser.add_argument("--interval", type=float, required=True, help="seconds between presses")
    parser.add_argument(
        "--settle", type=float, required=True, help="seconds to wait after creating the keyboard"
    )
    parser.add_argument(
        "--read-timeout", type=float, required=True, help="seconds a request may take"
    )
    parser.add_argument(
        "--touchscreens", required=True, help="the folder of the input devices, /dev/input"
    )
    parser.add_argument(
        "--displays", required=True, help="the folder of the display connectors, /sys/class/drm"
    )
    parser.add_argument(
        "--rescan", type=float, required=True, help="seconds between looks for touch screens"
    )
    args = parser.parse_args(argv)
    listener = listening_socket(dict(os.environ), os.getpid())
    keyboard = Keyboard()
    touchscreens = Touchscreens(args.touchscreens, args.rescan)
    try:
        time.sleep(args.settle)  # so that the compositor has the keyboard before its first key
        print("ai-robot-wake: ready", file=sys.stderr, flush=True)
        serve(
            listener,
            keyboard.press,
            interval=args.interval,
            read_timeout=args.read_timeout,
            dark=functools.partial(dark, args.displays),
            touchscreens=touchscreens,
        )
    finally:
        touchscreens.close()
        keyboard.close()


if __name__ == "__main__":
    main()
