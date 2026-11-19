# SPDX-License-Identifier: MIT
"""Calls to Varlink services, such as systemd's, on a Unix socket.

Each message is a JSON object, ended by one NUL byte; a call names its ``method`` and its
``parameters``, and its reply holds ``parameters``, or ``error`` if it failed
(varlink.org: Protocol, and Method Call; read 2026-11-19).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from ai_robot.core.data import read_json

END = b"\0"  # what ends each message


class VarlinkError(Exception):
    """The service answered with an error, or not as Varlink does."""


async def call(address: str, method: str, parameters: Mapping[str, Any] | None = None) -> Any:
    """Call ``method`` (``interface.Method``) of the service at the Unix socket
    ``address``; the parameters of its reply. ``OSError`` if the service cannot be
    reached; ``VarlinkError`` if it answers with an error."""
    reader, writer = await asyncio.open_unix_connection(address)
    try:
        message = {"method": method, "parameters": dict(parameters or {})}
        writer.write(json.dumps(message).encode() + END)
        await writer.drain()
        try:
            reply = read_json((await reader.readuntil(END))[: -len(END)])
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
            raise VarlinkError(f"{method}: no reply as Varlink sends one") from None
    finally:
        writer.close()
        await writer.wait_closed()
    if not isinstance(reply, dict) or not isinstance(reply.get("parameters", {}), dict):
        raise VarlinkError(f"{method}: no reply as Varlink sends one")
    if "error" in reply:
        raise VarlinkError(f"{method} failed")
    return reply.get("parameters", {})
