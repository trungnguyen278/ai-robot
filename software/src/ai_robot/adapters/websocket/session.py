# SPDX-License-Identifier: MIT
"""A session of the control channel (platform/protocols/cloud/README.md, Resuming a
session).

A session runs from the cloud's ``hello`` that opens it to a ``bye``, across as many
connections as it needs. It numbers what the robot sends, and keeps it until the cloud
has received it, so that the next connection can send it again. It takes the cloud's
messages once each and in order, at the rate the robot allows, and answers them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from ai_robot.adapters.websocket.mcp import McpServer
from ai_robot.core.data import Schema, write_json

if TYPE_CHECKING:
    from ai_robot.adapters.websocket.cloud import WebSocketCloud

log = logging.getLogger(__name__)

# Fixed by the protocol (platform/protocols/cloud/README.md and schema.json).
MAX_MESSAGE = 1 << 20  # bytes in a message
MAX_ERROR = 1024  # characters in an error's message

# How this implementation works, which nobody needs to tune.
DROP_REPORT = 10.0  # seconds between two reports of dropped messages
# What a log names once that the robot ignores (kinds of message, senders), at most: the
# other side chooses them, and remembering each would hold memory without end.
MAX_WARNED = 32


class Carrier(Protocol):
    """The connection that carries a session."""

    def write(self, text: str) -> None:
        """Send ``text`` after what was written before it."""


class Bucket:
    """A token bucket: ``rate`` messages a second, in bursts of up to ``burst``."""

    def __init__(self, rate: float, burst: int) -> None:
        self.rate = rate
        self.burst = burst
        self._tokens = float(burst)
        self._at = time.monotonic()

    async def take(self) -> None:
        """Wait for a token, then use it."""
        while True:
            now = time.monotonic()
            self._tokens = min(self.burst, self._tokens + (now - self._at) * self.rate)
            self._at = now
            if self._tokens >= 1:
                self._tokens -= 1
                return
            await asyncio.sleep((1 - self._tokens) / self.rate)


class Session:
    """A session with the cloud, which ``link`` holds.

    ``id`` is the cloud's identifier of the session; ``number`` tells its calls from those
    of the link's other sessions; ``schema`` checks the cloud's messages. ``connection``
    is the connection that carries the session now, if any: what the session sends goes
    out on it at once, and is kept for the next connection otherwise.
    """

    def __init__(self, link: WebSocketCloud, session_id: str, number: int, schema: Schema) -> None:
        self.link = link
        self.id = session_id
        self.schema = schema
        self.events: frozenset[str] = frozenset()  # the events the cloud subscribed to
        self.conversations = False  # whether both sides hold conversations
        self.protocol = (0, 0)  # the version of the link the cloud speaks, from its hello
        self.terms: dict[str, Any] = {}  # what the robot's hello that opened it set
        self.leave: int | None = None  # the number of the robot's leave, while one is asked
        self.sent = 0  # the number of the last message the robot sent
        self.received = 0  # the number of the last message received from the cloud
        self.kept: deque[tuple[int, str]] = deque()  # sent, and not yet received
        self.connection: Carrier | None = None
        self.mcp = McpServer(
            number=number,
            catalogue=lambda: link.capabilities,
            catalogue_ttl=link.catalogue_ttl,
            send=self._send_mcp,
            called=link.called,
            cancelled=link.cancelled,
        )
        self._inbox: asyncio.Queue[tuple[int, Mapping[str, Any]]] = asyncio.Queue(
            maxsize=link.backlog
        )
        self._bucket = Bucket(link.rate, link.burst)
        self._dropped = 0  # messages dropped since the last report
        self._reported = -DROP_REPORT  # when dropped messages were last reported
        self._warned: set[str] = set()
        self._handler = asyncio.create_task(self._handle_all(), name=f"cloud session {session_id}")

    async def close(self) -> None:
        """The session is over: stop handling the cloud's messages."""
        self._handler.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._handler

    # ---- sending ----

    def send(self, message: dict[str, Any]) -> int | None:
        """Number ``message``, keep it until the cloud has received it, and send it now
        if a connection carries the session. Returns its number, or ``None`` if the cloud
        cannot take it, which is logged, and not sent: it is not JSON that every side can
        hold (ai-platform's ADR-0014), or it is larger than the protocol allows."""
        number = self.sent + 1
        try:
            text = write_json({"type": message["type"], "seq": number, **message})
        except ValueError as error:
            log.error(
                "a %s message is not JSON that every side can hold (%s): not sent",
                message["type"],
                error,
            )
            return None
        if len(text) > MAX_MESSAGE:  # write_json() writes ASCII only: a byte a character
            log.error("a %s message is larger than the protocol allows: not sent", message["type"])
            return None
        self.sent = number
        self.kept.append((number, text))
        if len(self.kept) > self.link.replay:
            self.kept.popleft()  # the session can no longer resume before it
        if self.connection is not None:
            self.connection.write(text)
        return number

    def _send_mcp(self, payload: dict[str, Any], reply_to: int | None) -> bool:
        message: dict[str, Any] = {"type": "mcp"}
        if reply_to is not None:
            message["reply_to"] = reply_to
        message["payload"] = payload
        return self.send(message) is not None

    def error(self, reply_to: int, code: str, text: str) -> None:
        """Answer the cloud's message ``reply_to`` with an error."""
        message = {"type": "error", "reply_to": reply_to, "code": code, "message": text[:MAX_ERROR]}
        self.send(message)

    def emit(self, name: str, data: Mapping[str, Any], when: float) -> None:
        """Send the event ``name``, which happened ``when``, if the cloud subscribed to
        it."""
        if name in self.events:
            self.send({"type": "event", "name": name, "time": instant(when), "data": data})

    def changed(self, fields: Mapping[str, Any]) -> None:
        """Tell the cloud what the owner changed: ``device.changed``."""
        self.send({"type": "device.changed", **fields})

    def acknowledge(self, received: int) -> None:
        """The cloud has received every message up to ``received``: forget them."""
        while self.kept and self.kept[0][0] <= received:
            self.kept.popleft()

    def unreceived(self, received: int) -> list[str] | None:
        """What the cloud has not received, when it has received up to ``received``: to
        send again, in order. ``None`` if the session no longer holds all of it."""
        if received > self.sent:
            return None  # the cloud speaks of messages the robot never sent
        self.acknowledge(received)
        if len(self.kept) < self.sent - received:
            return None
        return [text for _, text in self.kept]

    # ---- receiving ----

    def receive(self, seq: int, message: Mapping[str, Any]) -> bool:
        """The cloud's message number ``seq``, in the order it came. Returns ``False``
        if it skipped a number: the session must end."""
        if seq <= self.received:
            return True  # sent again on a new connection, and taken already (SES-6)
        if seq != self.received + 1:
            return False
        self.received = seq
        try:
            self._inbox.put_nowait((seq, message))
        except asyncio.QueueFull:
            self._drop(seq)
        return True

    def _drop(self, seq: int) -> None:
        """Drop the message ``seq``: too many wait their turn. Say so now and then."""
        self._dropped += 1
        now = time.monotonic()
        if now - self._reported >= DROP_REPORT:
            self._reported = now
            what = "too many messages; some were dropped"
            log.warning("dropping messages from the cloud: %s (%d)", what, self._dropped)
            self._dropped = 0
            self.error(seq, "rate_limited", what)

    async def _handle_all(self) -> None:
        while True:
            seq, message = await self._inbox.get()
            await self._bucket.take()
            try:
                await self._handle(seq, message)
            except Exception:  # a failure must not end the session
                log.exception("handling the cloud's message %d failed", seq)
                self.error(seq, "internal", "the robot failed to handle the message")

    async def _handle(self, seq: int, message: Mapping[str, Any]) -> None:
        problem = self.schema.problem(message)
        if problem is not None:
            log.warning("the cloud's message %d is not valid: %s", seq, problem)
            self.error(seq, "invalid_message", problem)
            return
        kind = message["type"]
        conversations = self.link.conversations
        if kind == "mcp":
            self.mcp.handle(message["payload"], seq)
        elif kind == "conversation.start":
            await conversations.on_start(seq, message)
        elif kind == "conversation.end":
            await conversations.on_end(seq, message)
        elif kind == "error":
            if self.leave is not None and message["reply_to"] == self.leave:
                self.link.refused_leave(self, message["code"])
            elif not await conversations.on_error(message):
                # The cloud's text, quoted: a line break in it must not start a log line.
                log.warning(
                    "the cloud reports %s about message %d: %r",
                    message["code"],
                    message["reply_to"],
                    message["message"],
                )
        elif kind not in self._warned and len(self._warned) < MAX_WARNED:
            self._warned.add(kind)
            log.info("ignoring %s messages from the cloud", kind)


def instant(when: float) -> str:
    """``when``, in seconds since the epoch, in RFC 3339 and UTC, to the millisecond."""
    moment = datetime.fromtimestamp(when, UTC).isoformat(timespec="milliseconds")
    return moment.replace("+00:00", "Z")
