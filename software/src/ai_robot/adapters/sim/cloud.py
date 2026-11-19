# SPDX-License-Identifier: MIT
"""A simulated cloud: the robot runs and is tested without ai-cloud."""

from __future__ import annotations

import asyncio
import itertools
from collections import deque
from collections.abc import Mapping, Sequence
from typing import Any

from ai_robot.ports.cloud import (
    CallCancelled,
    Capability,
    Cloud,
    CloudPart,
    ConversationAudio,
    ConversationEnd,
    ConversationRequest,
    ConversationStart,
    DeviceInfo,
    EventKind,
    LinkStatus,
    RobotPart,
    ToolCall,
    check_url,
)
from ai_robot.ports.identity import Identity

Outcome = tuple[Mapping[str, Any] | None, str | None]  # the result's data, or an error


class SimCloud(Cloud):
    """A cloud that is always there and asks for nothing on its own.

    It opens a session as soon as the robot says what it is (kept in ``device``) and
    offers what it can do, and subscribes to every event, which it keeps in ``events``.
    If the robot offers conversations, it takes them: it starts each one the robot asks
    for, and keeps the robot's part of them in ``heard``. It calls a capability, opens a
    conversation or says its own part of one only when told to (``call``, ``begin``,
    ``open_conversation``, ``say``), as tests do. Its server knows the robot,
    whichever it is asked to ``serve``, until a test says otherwise (``forget``,
    ``recognise``). It leaves the robot's home at once, as the server forgets it,
    unless a test makes it unreachable for that (``answers_leave``).
    """

    def __init__(self) -> None:
        self.capabilities: dict[str, Capability] = {}
        self.event_kinds: dict[str, EventKind] = {}
        self.events: deque[tuple[str, Mapping[str, Any]]] = deque(maxlen=100)
        self.heard: deque[RobotPart] = deque(maxlen=1000)
        self.online = False
        self.conversing = False
        self.device: DeviceInfo | None = None
        self.may_listen = True  # whether the robot's microphones are on
        self._offered = False
        self.identity: Identity | None = None
        self.server: str | None = None  # the server the robot was registered with, if any
        self.knows_robot = True  # set it to False before the robot starts for one not set up
        self.answers_leave = True  # set it to False for a leave that gets no outcome
        self.left = False  # whether the robot left its home
        self._requests: asyncio.Queue[ToolCall | CallCancelled] = asyncio.Queue()
        self._statuses: asyncio.Queue[LinkStatus] = asyncio.Queue()
        self._said: asyncio.Queue[CloudPart] = asyncio.Queue()
        self._outcomes: dict[str, asyncio.Future[Outcome]] = {}
        self._ids = itertools.count(1)

    async def identify(self, identity: Identity) -> None:
        self.identity = identity

    async def serve(self, server: str) -> None:
        self.server = check_url("server", server, ("https", "http"))

    async def describe(self, device: DeviceInfo) -> None:
        self.device = device
        if not self.online and self._offered:
            self._connect()

    async def listening(self, allowed: bool) -> None:
        self.may_listen = allowed

    async def offer(
        self,
        capabilities: Sequence[Capability],
        events: Sequence[EventKind],
        *,
        conversation: ConversationAudio | None = None,
    ) -> None:
        self.capabilities = {capability.name: capability for capability in capabilities}
        if not self.online:
            self.event_kinds = {kind.name: kind for kind in events}
            self.conversing = conversation is not None
            self._offered = True
            self._connect()

    def _connect(self) -> None:
        """Open a session, once the robot has said what it is."""
        if self.device is not None:
            self.online = self.knows_robot  # a server that does not know the robot opens no session
            self._statuses.put_nowait(self._status())

    async def request(self) -> ToolCall | CallCancelled:
        return await self._requests.get()

    async def reply(
        self,
        call: ToolCall,
        *,
        data: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        outcome = self._outcomes.get(call.id)
        if outcome is not None and not outcome.done():
            outcome.set_result((None, error) if error is not None else (data or {}, None))

    async def emit(self, name: str, data: Mapping[str, Any]) -> None:
        if self.online and name in self.event_kinds:
            self.events.append((name, data))

    async def status(self) -> LinkStatus:
        return await self._statuses.get()

    def _status(self) -> LinkStatus:
        if not self.online:
            return LinkStatus(False, bound=self.knows_robot)
        events = frozenset(self.event_kinds)
        return LinkStatus(True, "sim", events, self.conversing, bound=self.knows_robot)

    def forget(self) -> None:
        """The server no longer knows the robot, as if its owner revoked it: the session
        ends."""
        self.knows_robot = False
        self.online = False
        self._statuses.put_nowait(self._status())

    def recognise(self) -> None:
        """The server knows the robot again, as after set-up: a session opens."""
        self.knows_robot = True
        self.online = True
        self._statuses.put_nowait(self._status())

    async def converse(self, part: RobotPart) -> None:
        if not (self.online and self.conversing):
            if isinstance(part, ConversationRequest):
                self._said.put_nowait(ConversationEnd(part.conversation, "unavailable"))
            return
        self.heard.append(part)
        if isinstance(part, ConversationRequest):
            self._said.put_nowait(ConversationStart(part.conversation, listen=True))

    async def conversation(self) -> CloudPart:
        return await self._said.get()

    async def leave(self) -> bool:
        if not self.answers_leave:
            return False
        self.left = True
        if self.knows_robot:
            self.forget()
        return True

    def open_conversation(self, conversation: str, *, listen: bool) -> None:
        """Open a conversation, as the cloud would for a reminder."""
        self._said.put_nowait(ConversationStart(conversation, listen))

    def say(self, *parts: CloudPart) -> None:
        """Say ``parts`` of a conversation, as the cloud would."""
        for part in parts:
            self._said.put_nowait(part)

    def begin(self, name: str, arguments: Mapping[str, Any] | None = None) -> ToolCall:
        """Call a capability as the cloud would, without waiting for the outcome."""
        call = ToolCall(f"sim-{next(self._ids)}", name, arguments or {})
        self._outcomes[call.id] = asyncio.get_running_loop().create_future()
        self._requests.put_nowait(call)
        return call

    async def outcome(self, call: ToolCall, timeout: float = 5.0) -> Outcome:
        """Wait for the robot's answer to ``call``: the result's data, or an error."""
        try:
            return await asyncio.wait_for(asyncio.shield(self._outcomes[call.id]), timeout)
        finally:
            self._outcomes.pop(call.id, None)

    async def call(
        self, name: str, arguments: Mapping[str, Any] | None = None, timeout: float = 5.0
    ) -> Outcome:
        """Call a capability as the cloud would, and wait for the robot's answer."""
        if name not in self.capabilities:
            return None, f"unknown tool {name}"  # what the cloud's MCP client would get
        return await self.outcome(self.begin(name, arguments), timeout)

    def cancel(self, call: ToolCall) -> None:
        """Tell the robot that the cloud no longer wants the result of ``call``."""
        self._outcomes.pop(call.id, None)
        self._requests.put_nowait(CallCancelled(call.id))
