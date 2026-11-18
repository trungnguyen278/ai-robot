# SPDX-License-Identifier: MIT
"""How long the cloud asks the robot to wait before it asks again, after a refusal of its
token endpoint or of the control channel's handshake (platform/protocols/cloud/README.md,
Tokens from the server and The control channel)."""

from __future__ import annotations

import email.utils
from collections.abc import Mapping
from datetime import UTC, datetime

# Fixed by the protocol (platform/protocols/cloud/README.md, schema.json).
LONGEST_WAIT = 86400.0  # seconds: the longest wait the protocol speaks of ($defs/seconds)


def retry_after(headers: Mapping[str, str] | None) -> float | None:
    """The wait a refusal's ``Retry-After`` asks for, in seconds or as a date (RFC 9110,
    §10.2.3), within the longest the protocol speaks of; ``None`` if it asks for none, or
    for one the robot cannot read."""
    value = headers.get("Retry-After") if headers else None
    if not value:
        return None
    value = value.strip(" \t")  # RFC 9110, §5.5: whitespace around a value is not in it
    if value.isascii() and value.isdigit():
        # delay-seconds = 1*DIGIT: float() reads any number of them, past a double's range
        # as an infinity, where int() takes at most sys.int_info's 4,300.
        seconds = float(value)
    else:
        try:
            when = email.utils.parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            seconds = (when - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, IndexError, OverflowError):
            return None  # a date past datetime's years 1 to 9999 overflows
    return min(max(seconds, 0.0), LONGEST_WAIT)
