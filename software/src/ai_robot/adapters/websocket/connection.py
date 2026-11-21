# SPDX-License-Identifier: MIT
"""One connection of the control channel: a WebSocket, from its handshake to its close
(platform/protocols/cloud/README.md, The control channel, Staying connected and
Messages)."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import random
import re
from dataclasses import dataclass
from typing import Any

import aiohttp
from aiohttp import WSCloseCode, WSMsgType

from ai_robot.adapters.websocket.session import Session
from ai_robot.core.data import Schema, encode, read_json, write_json

log = logging.getLogger(__name__)

# Fixed by the protocol (platform/protocols/cloud/README.md, Messages and schema.json).
PROTOCOL = "3.3"  # the version of the link that the robot speaks
LEAVE_SINCE = (3, 2)  # the first version whose cloud takes a leave (leave)
UNNUMBERED = frozenset({"hello", "ping", "pong", "reconnect", "bye"})
LARGEST_SEQ = 2**53 - 1  # $defs/seq: what JSON numbers hold exactly
MAX_BYE = 256  # characters in a bye's message ($defs/short_text)
UNANSWERABLE = "invalid_message"  # why a session ends over a frame that cannot be answered
# What the robot's bye no_hello adds, for the cloud's logs: which of its two causes it had
# (The session, hello; the cloud says the same, examples/first-message.json).
NOT_FIRST = "the first message was not a hello"
NOT_IN_TIME = "the hello did not come in time"
_VERSION = re.compile(r"([0-9]{1,3})\.([0-9]{1,3})")  # $defs/version


@dataclass(frozen=True, slots=True)
class Ending:
    """How a connection ended.

    - ``greeted``: whether its hello exchange completed, which starts the backoff
      again;
    - ``reason``: why the session ended, from either side, if it did: a ``bye``'s reason,
      or ``invalid_message`` for a frame that the connection closed over, after which no
      bye can follow (Messages, Sizes). The session cannot resume after it;
    - ``by_cloud``: whether the cloud ended the session;
    - ``message``: what the robot's ``bye`` adds, for the cloud's logs.
    """

    greeted: bool
    reason: str | None = None
    by_cloud: bool = False
    message: str | None = None


@dataclass(frozen=True, slots=True)
class Message:
    """A message from the cloud that the robot can read: a JSON object, ``body``, whose
    ``type`` is a string. ``seq`` is its number, ``None`` for the types that have none."""

    type: str
    seq: int | None
    body: dict[str, Any]


class Connection:
    """The WebSocket ``ws`` of the control channel, whose messages ``schema`` checks.

    Before it carries a session, the robot says its hello on it (``say``) and reads the
    cloud's (``first``). Then it carries the session (``carry``): it writes what the
    session sends, in order (``write``), reads what the cloud sends, and pings. It
    closes after the robot's ``bye`` (``finish``, or ``end``).
    """

    def __init__(self, ws: aiohttp.ClientWebSocketResponse, schema: Schema, close_timeout: float):
        self.ws = ws
        self.schema = schema
        self.close_timeout = close_timeout
        self._outbox: asyncio.Queue[str | None] = asyncio.Queue()  # None: close here
        self._writer: asyncio.Task[None] | None = None
        self._bye: str | None = None  # the reason of the robot's bye, once it says it
        self._pong_due = False
        self._warned: set[str] = set()

    async def say(self, message: dict[str, Any]) -> None:
        """Send ``message`` at once, before the connection carries a session.
        ``ValueError`` if it is not JSON that every side can hold."""
        await self.ws.send_str(write_json(message))

    async def first(self) -> dict[str, Any] | Ending:
        """The cloud's hello, valid and of the robot's major version. Otherwise how the
        connection ends, before its hello exchange completed; ``end`` then says the
        robot's bye, if it must (The session, hello)."""
        message = await self._next()
        if isinstance(message, Ending):
            return dataclasses.replace(message, greeted=False)
        if message.type == "bye":
            return dataclasses.replace(self._cloud_bye(message.body), greeted=False)
        if message.type == "hello":
            version = message.body.get("protocol")
            major = _VERSION.fullmatch(version) if isinstance(version, str) else None
            if major is not None and int(major[1]) != int(PROTOCOL.split(".")[0]):
                # Another major's hello need not match this one's schema.
                log.warning("the cloud speaks the link %s, not %s", version, PROTOCOL)
                return Ending(greeted=False, reason="unsupported_protocol")
        failing = self._failing(message)
        if failing is not None:
            return dataclasses.replace(failing, greeted=False)
        if message.type != "hello":
            # A numbered message's type is the cloud's text until the schema passes it.
            problem = self.schema.problem(message.body)
            if problem is None:
                log.warning("a %s message came before the cloud's hello", message.type)
            else:
                log.warning("a message came before the cloud's hello: %s", problem)
            return Ending(greeted=False, reason="no_hello", message=NOT_FIRST)
        return message.body

    def write(self, text: str) -> None:
        """Send ``text`` after what was written before it."""
        if self._bye is None:
            self._outbox.put_nowait(text)

    async def finish(self, reason: str, message: str | None = None) -> None:
        """Say ``bye`` with ``reason``, and ``message`` if any, after what was written
        before it, then close. On a connection closed already, the bye goes nowhere: the
        writer stops, or the send fails, and the reason still ends the session."""
        if self._bye is not None:
            return
        self._bye = reason
        said = {"type": "bye", "reason": reason}
        if message is not None:
            said["message"] = message[:MAX_BYE]
        bye = encode(said)
        if self._writer is None or self._writer.done():
            with contextlib.suppress(Exception):  # the connection may be gone already
                await self.ws.send_str(bye)
            await self.ws.close()
            return
        self._outbox.put_nowait(bye)
        self._outbox.put_nowait(None)
        # The writer closes the connection after the bye; the reader then ends, and the
        # writer may be cancelled with it, its work done.
        await asyncio.wait({self._writer}, timeout=self.close_timeout)

    async def end(self, ending: Ending) -> Ending:
        """End the connection as ``ending`` says: with the robot's bye, if the robot ends
        the session. Returns ``ending``."""
        if ending.reason is not None and not ending.by_cloud:
            await self.finish(ending.reason, ending.message)
        return ending

    async def carry(self, session: Session, interval: float) -> Ending:
        """Carry ``session`` until the connection ends, pinging every ``interval``
        seconds."""
        self._writer = asyncio.create_task(self._write(), name="cloud: writing")
        reading = asyncio.create_task(self._read(session), name="cloud: reading")
        pinging = asyncio.create_task(self._ping(session, interval), name="cloud: pinging")
        tasks = {self._writer, reading, pinging}
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            ending = reading.result() if reading in done else None
            if ending is not None:
                await self.end(ending)
            if self._bye is not None:
                return Ending(greeted=True, reason=self._bye)
            return ending or Ending(greeted=True)
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await self.ws.close()

    # ---- the three tasks of a connection ----

    async def _write(self) -> None:
        while True:
            text = await self._outbox.get()
            if text is None:
                await self.ws.close()
                return
            try:
                await self.ws.send_str(text)
            except Exception as error:  # the connection is gone; the reader sees it too
                log.info("cannot send to the cloud: %s", error or type(error).__name__)
                return

    async def _read(self, session: Session) -> Ending:
        while True:
            message = await self._next()
            if isinstance(message, Ending):
                return message
            ending = self._take(message, session)
            if ending is not None:
                return ending

    def _take(self, message: Message, session: Session) -> Ending | None:
        """Take one message from the cloud. Returns how the connection ends, if it does."""
        if message.type == "bye":
            return self._cloud_bye(message.body)
        if message.seq is not None:
            if not session.receive(message.seq, message.body):
                log.warning(
                    "the cloud's message %d skips a number after %d", message.seq, session.received
                )
                return Ending(greeted=True, reason="out_of_sequence")
            return None
        failing = self._failing(message)
        if failing is not None:
            return failing
        if message.type == "pong":
            self._pong_due = False
            session.acknowledge(message.body["received"])
        elif message.type == "reconnect":
            log.info("the cloud asks the robot to connect again")
            return Ending(greeted=True)
        else:
            self._once(message.type, f"ignoring {message.type} messages from the cloud now")
        return None

    async def _ping(self, session: Session, interval: float) -> None:
        """Ping every ``interval`` seconds, and return when a pong is late."""
        await asyncio.sleep(random.uniform(0, interval))  # devices that connect together spread
        while True:
            if self._pong_due:
                log.warning("the cloud did not answer a ping within %g s", interval)
                return
            self._pong_due = True
            self.write(encode({"type": "ping", "received": session.received}))
            await asyncio.sleep(interval)

    # ---- reading frames ----

    async def _next(self) -> Message | Ending:
        """The cloud's next message, if the robot can read it; otherwise how the
        connection ends (Messages, Validation and Sizes)."""
        frame = await self.ws.receive()
        if frame.type is WSMsgType.TEXT:
            try:
                body = read_json(frame.data)
            except ValueError as error:
                return self._cannot_answer(f"not JSON that every side can hold: {error}")
            if not isinstance(body, dict):
                return self._cannot_answer("not a JSON object")
            kind = body.get("type")
            if not isinstance(kind, str):
                return self._cannot_answer("no type that is a string")
            if kind in UNNUMBERED:
                return Message(kind, None, body)
            seq = _seq(body)
            if seq is None:
                return self._cannot_answer("a numbered type without its number")
            return Message(kind, seq, body)
        if frame.type is WSMsgType.BINARY:
            return self._cannot_answer("a binary frame")
        if frame.type is WSMsgType.ERROR and isinstance(frame.data, aiohttp.WebSocketError):
            # aiohttp could not read a frame, and closed the connection with the error's
            # code: 1009 for a message larger than the protocol allows, 1007 for text that
            # is not UTF-8. No bye can follow, and the session ends (Messages, Sizes).
            code = frame.data.code
            log.warning("the cloud sent a frame that the robot cannot read (%d)", code)
            return Ending(greeted=True, reason=UNANSWERABLE)
        if frame.type is WSMsgType.CLOSE and frame.data == WSCloseCode.MESSAGE_TOO_BIG:
            log.warning("the robot sent the cloud a message larger than it takes")
            return Ending(greeted=True, reason=UNANSWERABLE, by_cloud=True)
        return Ending(greeted=True)  # closed, or failed: the session may resume

    def _failing(self, message: Message) -> Ending | None:
        """How the session ends over ``message``, if it has no number and fails the
        schema: no error can answer it (Messages, Validation)."""
        if message.seq is not None:
            return None
        problem = self.schema.problem(message.body)
        return None if problem is None else self._cannot_answer(f"{message.type}: {problem}")

    def _cannot_answer(self, why: str) -> Ending:
        """End the session over a frame that the robot cannot answer: ``why``, which names
        no value, goes in the robot's bye (Messages, Validation)."""
        log.warning("the cloud sent a frame that the robot cannot answer: %s", why)
        return Ending(greeted=True, reason=UNANSWERABLE, message=why)

    def _cloud_bye(self, bye: dict[str, Any]) -> Ending:
        """The cloud's bye ends the session, whatever it holds: a reason that the robot
        cannot read, it takes as ``error``, as it takes one it does not know (Messages)."""
        problem = self.schema.problem(bye)
        if problem is not None:
            log.warning("the cloud's bye is not valid: %s", problem)
            return Ending(greeted=True, reason="error", by_cloud=True)
        return Ending(greeted=True, reason=bye["reason"], by_cloud=True)

    def _once(self, key: str, text: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(text)


def version(text: str) -> tuple[int, int]:
    """A version of the link that the schema allowed, such as ``3.2``, as (major, minor)."""
    match = _VERSION.fullmatch(text)
    if match is None:
        raise ValueError("not a version of the link")
    return int(match[1]), int(match[2])


def _seq(message: dict[str, Any]) -> int | None:
    """A message's number, if it has one that the schema allows: an integer from 1, which
    JSON Schema also reads in a number such as ``1.0``."""
    seq = message.get("seq")
    if isinstance(seq, float) and seq.is_integer():
        seq = int(seq)
    if isinstance(seq, bool) or not isinstance(seq, int) or not 1 <= seq <= LARGEST_SEQ:
        return None
    return seq
