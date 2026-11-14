# SPDX-License-Identifier: MIT
"""ai-robot-wake: presses ``KEY_WAKEUP`` on a keyboard that has no other key, when the
robot asks (docs/decisions/0021-sleep-and-wake.md).

A dark screen lights up on input, and the robot has no other way to light it. The robot
gets no uinput access of its own: this helper creates a uinput keyboard with that one
key, and presses it at most once per ``--interval`` seconds, whoever asks how often.
It runs as its own service, with only the ``uinput`` group, on a socket that systemd
opens and that only the robot's group may use (tools/pi/kiosk/). It uses the standard
library only, so that tools/pi/kiosk.sh can install it on its own.

Each connection sends ``REQUEST`` and receives ``ANSWER_OK``, or ``ANSWER_UNKNOWN`` for
anything else.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import math
import os
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
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Answer the connections to ``listener`` one at a time, pressing at most once per
    ``interval`` seconds, until the listener is closed.

    A press that fails ends it: systemd starts the helper again, with a new keyboard."""
    last = -math.inf
    while True:
        try:
            connection, _ = listener.accept()
        except OSError:  # closed
            return
        with connection:
            connection.settimeout(read_timeout)
            try:
                request = read_request(connection)
            except OSError:  # it left, or was too slow: the next one
                continue
            answer = ANSWER_UNKNOWN
            if request == REQUEST:
                now = clock()
                if now - last >= interval:
                    press()
                    last = now
                answer = ANSWER_OK
            with contextlib.suppress(OSError):  # it left before the answer
                connection.sendall(answer)


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
    args = parser.parse_args(argv)
    listener = listening_socket(dict(os.environ), os.getpid())
    keyboard = Keyboard()
    try:
        time.sleep(args.settle)  # so that the compositor has the keyboard before its first key
        print("ai-robot-wake: ready", file=sys.stderr, flush=True)
        serve(listener, keyboard.press, interval=args.interval, read_timeout=args.read_timeout)
    finally:
        keyboard.close()


if __name__ == "__main__":
    main()
