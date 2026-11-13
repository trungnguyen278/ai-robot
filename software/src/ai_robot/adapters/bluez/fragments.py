# SPDX-License-Identifier: MIT
"""Messages split into Bluetooth fragments, and joined again (platform/protocols/setup/README.md,
The Bluetooth service)."""

from __future__ import annotations

from ai_robot.core.setup_session import MAX_MESSAGE

# Fixed by the protocol: the flags of a fragment's first byte, and the length after them.
FIRST = 0x80
LAST = 0x40
LENGTH_BYTES = 2
ATT_OVERHEAD = 3  # bytes of a write or an indication that are not its value (Bluetooth Core)
# The longest attribute value (Bluetooth Core, Vol 3, Part F, 3.2.9): BlueZ refuses a longer
# write, and cuts a longer indication (BT_ATT_MAX_VALUE_LEN in src/shared/att-types.h, 5.85).
MAX_VALUE = 512


class FragmentError(Exception):
    """A fragment that does not fit: the session must end."""


def split(message: bytes, mtu: int) -> list[bytes]:
    """``message`` in fragments that each fit a write or an indication on a link whose ATT
    MTU is ``mtu``, and an attribute's value."""
    if not message or len(message) > MAX_MESSAGE:
        raise ValueError(f"a message has 1 to {MAX_MESSAGE} bytes, got {len(message)}")
    room = min(mtu - ATT_OVERHEAD, MAX_VALUE) - 1  # the flags take a byte
    if room <= LENGTH_BYTES:
        raise ValueError(f"an ATT MTU of {mtu} leaves no room for a message")
    first = message[: room - LENGTH_BYTES]
    fragments = [bytes([FIRST]) + len(message).to_bytes(LENGTH_BYTES, "big") + first]
    rest = message[len(first) :]
    fragments += [bytes([0]) + rest[i : i + room] for i in range(0, len(rest), room)]
    fragments[-1] = bytes([fragments[-1][0] | LAST]) + fragments[-1][1:]
    return fragments


class Joiner:
    """Joins one peer's fragments into its messages. It holds the announced length at
    most, however many fragments come: one without bytes adds nothing."""

    def __init__(self) -> None:
        self._message = bytearray()
        self._expected = 0  # bytes of the message being joined; 0 between messages

    def add(self, fragment: bytes) -> bytes | None:
        """Take the next fragment; return the message it completes, if any.
        ``FragmentError`` for one that does not fit."""
        if not fragment or fragment[0] & ~(FIRST | LAST):
            raise FragmentError("a fragment has unknown flags")
        flags, data = fragment[0], fragment[1:]
        if flags & FIRST:
            if self._expected:
                raise FragmentError("a first fragment came within a message")
            if len(data) < LENGTH_BYTES:
                raise FragmentError("a first fragment is too short for the length")
            length = int.from_bytes(data[:LENGTH_BYTES], "big")
            if not 1 <= length <= MAX_MESSAGE:
                raise FragmentError(f"a message has 1 to {MAX_MESSAGE} bytes, not {length}")
            self._expected, self._message = length, bytearray()
            data = data[LENGTH_BYTES:]
        elif not self._expected:
            raise FragmentError("a fragment came with no first one")
        if len(self._message) + len(data) > self._expected:
            raise FragmentError("a message is longer than its length")
        self._message += data
        if not flags & LAST:
            return None
        if len(self._message) != self._expected:
            raise FragmentError("a message is shorter than its length")
        message = bytes(self._message)
        self._expected, self._message = 0, bytearray()
        return message
