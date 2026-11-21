# SPDX-License-Identifier: MIT
"""The link to ai-cloud: a WebSocket for the session, and a LiveKit room for each
conversation (platform/protocols/cloud/README.md).

Before each connection, the robot gets an access token (``tokens.py``). On the
connection (``connection.py``) it holds a session (``session.py``), which outlives a
lost connection: the robot connects again and resumes it. The cloud calls the robot's
capabilities over MCP (``mcp.py``), and the robot sends the events the cloud subscribed
to, and what its owner changes. Conversations are asked for and ended on the session
(``conversations.py``), and held in a room of their own (``room.py``). Before its
factory reset, the robot leaves its home (``leave``).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import random
import time
from collections import deque
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import aiohttp

from ai_robot import software_version
from ai_robot.adapters.websocket.connection import (
    LEAVE_SINCE,
    NOT_IN_TIME,
    PROTOCOL,
    Connection,
    Ending,
    version,
)
from ai_robot.adapters.websocket.conversations import Conversations
from ai_robot.adapters.websocket.retry import retry_after
from ai_robot.adapters.websocket.room import ConversationRoom
from ai_robot.adapters.websocket.session import MAX_MESSAGE, Session, instant
from ai_robot.adapters.websocket.tokens import NoToken, NotSetUp, access_token
from ai_robot.core.checks import finite, positive
from ai_robot.core.config import platform_dir
from ai_robot.core.data import Schema, plain
from ai_robot.ports.cloud import (
    CallCancelled,
    Capability,
    Cloud,
    CloudPart,
    ConversationAudio,
    DeviceInfo,
    EventKind,
    LinkStatus,
    RobotPart,
    ToolCall,
    check_url,
)
from ai_robot.ports.identity import Identity

log = logging.getLogger(__name__)

# Fixed by the protocol (platform/protocols/cloud/README.md, Connecting and The session).
LINK_PATH = "/device/v3/link"
MEDIA = "livekit"  # where conversations' audio goes


class Refused(Exception):
    """The cloud refused the WebSocket's handshake with ``status``, and may have said
    how long to wait before the next attempt (``retry_after``, in seconds)."""

    def __init__(self, status: int, retry_after: float | None) -> None:
        super().__init__(f"the cloud refused the connection ({status})")
        self.status = status
        self.retry_after = retry_after


class WebSocketCloud(Cloud):
    """The link to ai-cloud over a WebSocket (platform/protocols/cloud/README.md).

    ``server`` is the ai-cloud the robot connects to (``https://``, or ``http://`` to this
    computer), until set-up names the one it was registered with (``serve``). The robot
    proves its identity (``identify``) to get its access tokens.

    The other settings, in seconds unless they say, are explained in
    ``config/base.toml`` (``[ports.cloud.websocket]``).
    """

    def __init__(
        self,
        *,
        server: str,
        connect_timeout: float,
        hello_timeout: float,
        reconnect_first: float,
        reconnect_max: float,
        replay: int,
        rate: float,
        burst: int,
        backlog: int,
        catalogue_ttl: float,
        waiting_events: int,
        start_timeout: float,
        agent_timeout: float,
        leave_timeout: float,
    ) -> None:
        self.server = check_url("server", server, ("https", "http"))
        self.connect_timeout = positive("connect_timeout", connect_timeout)
        self.hello_timeout = positive("hello_timeout", hello_timeout)
        self.reconnect_first = positive("reconnect_first", reconnect_first)
        self.reconnect_max = positive("reconnect_max", reconnect_max)
        if self.reconnect_max < self.reconnect_first:
            raise ValueError("reconnect_max must not be below reconnect_first")
        self.replay = _count("replay", replay)
        self.rate = positive("rate", rate)
        self.burst = _count("burst", burst)
        self.backlog = _count("backlog", backlog)
        self.catalogue_ttl = finite("catalogue_ttl", catalogue_ttl)
        if self.catalogue_ttl < 0:
            raise ValueError(f"catalogue_ttl must not be negative, got {catalogue_ttl}")
        self.start_timeout = positive("start_timeout", start_timeout)
        self.agent_timeout = positive("agent_timeout", agent_timeout)
        self.leave_timeout = positive("leave_timeout", leave_timeout)
        self.capabilities: dict[str, Capability] = {}
        self.event_kinds: list[EventKind] = []
        self.audio: ConversationAudio | None = None  # offered for conversations, if any
        self.device: DeviceInfo | None = None
        self.may_listen = True  # whether the robot's microphones are on
        self.identity: Identity | None = None
        self.bound: bool | None = None  # what the server last said of the robot
        self.update_required = False
        self.session: Session | None = None  # the session, open or to resume
        self.connection: Connection | None = None  # the connection that carries it
        self.schema: Schema | None = None
        self.agent_schema: Schema | None = None  # what agents say in a room
        self._token_schema: Schema | None = None
        self.conversations = Conversations(self)
        self.room_factory: Any = ConversationRoom  # tests replace it
        self._http: aiohttp.ClientSession | None = None
        self._numbers = itertools.count(1)
        self._described = asyncio.Event()
        self._offered = asyncio.Event()
        # Events that happened while no connection carried a session: (name, data, when).
        self._waiting: deque[tuple[str, Mapping[str, Any], float]] = deque(
            maxlen=_count("waiting_events", waiting_events)
        )
        self._requests: asyncio.Queue[ToolCall | CallCancelled] = asyncio.Queue()
        self._statuses: asyncio.Queue[LinkStatus] = asyncio.Queue()
        self._heard: asyncio.Queue[CloudPart] = asyncio.Queue()
        self._reported: LinkStatus | None = None
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._leaving: asyncio.Future[bool] | None = None  # once asked: the leave's outcome
        self._leave_refusals = 0  # leaves the cloud failed, for the backoff before the next
        self._asking: asyncio.TimerHandle | None = None  # the next leave, after a refusal
        self._hurry = asyncio.Event()  # the next attempt to connect skips the backoff

    async def open(self) -> None:
        self.schema, self.agent_schema, self._token_schema = await asyncio.to_thread(
            _load_schemas, platform_dir() / "cloud" / "schema.json"
        )
        traces = aiohttp.TraceConfig()
        traces.on_request_redirect.append(_refuse_redirect)
        self._http = aiohttp.ClientSession(trace_configs=[traces])
        self._task = asyncio.create_task(self._run(), name="cloud link")

    async def close(self) -> None:
        self._closing = True
        if self._asking is not None:
            self._asking.cancel()
        session, connection = self.session, self.connection
        if session is not None and connection is not None:
            with contextlib.suppress(Exception):
                session.mcp.end()
                await connection.finish("stopping")
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self.conversations.close()
        if self.session is not None:
            await self.session.close()
        if self._http is not None:
            await self._http.close()

    # ---- the port ----

    async def identify(self, identity: Identity) -> None:
        self.identity = identity

    async def serve(self, server: str) -> None:
        self.server = check_url("server", server, ("https", "http"))

    async def describe(self, device: DeviceInfo) -> None:
        before, self.device = self.device, device
        self._described.set()
        session = self.session
        if before is not None and session is not None and self.connection is not None:
            changes = _changes(before, device)
            if changes:
                session.changed(changes)
        # Otherwise the next hello says it.

    async def listening(self, allowed: bool) -> None:
        self.may_listen = allowed

    async def offer(
        self,
        capabilities: Sequence[Capability],
        events: Sequence[EventKind],
        *,
        conversation: ConversationAudio | None = None,
    ) -> None:
        offered = {capability.name: capability for capability in capabilities}
        changed = offered != self.capabilities
        self.capabilities = offered
        self.event_kinds = list(events)
        self.audio = conversation
        self._offered.set()
        if changed and self.session is not None:
            self.session.mcp.catalogue_changed()

    async def request(self) -> ToolCall | CallCancelled:
        return await self._requests.get()

    async def reply(
        self,
        call: ToolCall,
        *,
        data: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        if self.session is not None:
            self.session.mcp.reply(call, data, error)

    async def emit(self, name: str, data: Mapping[str, Any]) -> None:
        when = time.time()
        if self.session is not None and self.connection is not None:
            self.session.emit(name, data, when)
        else:
            self._waiting.append((name, data, when))  # the oldest go first (REL-3)

    async def status(self) -> LinkStatus:
        return await self._statuses.get()

    async def converse(self, part: RobotPart) -> None:
        await self.conversations.converse(part)

    async def conversation(self) -> CloudPart:
        return await self._heard.get()

    async def leave(self) -> bool:
        try:
            async with asyncio.timeout(self.leave_timeout):
                if self._leaving is None:
                    self._leaving = asyncio.get_running_loop().create_future()
                    log.info("the robot leaves its home")
                    await self.conversations.end("user")
                    if self.bound is False:
                        self._left()  # its server no longer knows it
                    else:
                        self._ask_to_leave()
                        if self.connection is None:
                            self._hurry.set()
                return await asyncio.shield(self._leaving)
        except TimeoutError:
            log.warning("the robot could not leave its home within %g s", self.leave_timeout)
            return False

    @property
    def leaving(self) -> bool:
        """Whether the robot asked to leave its home: it holds no conversation from then."""
        return self._leaving is not None

    # ---- what the session tells the link ----

    def called(self, call: ToolCall) -> None:
        self._requests.put_nowait(call)

    def cancelled(self, call: str) -> None:
        self._requests.put_nowait(CallCancelled(call))

    def heard(self, part: CloudPart) -> None:
        self._heard.put_nowait(part)

    def refused_leave(self, session: Session, code: str) -> None:
        """The cloud failed to remove the robot from its home: ask again after the
        backoff, on the session then carried."""
        session.leave = None
        self._leave_refusals += 1
        delay = self._backoff(self._leave_refusals)
        log.warning(
            "the cloud could not remove the robot (%s): asking again in %.1f s", code, delay
        )
        self._asking = asyncio.get_running_loop().call_later(delay, self._ask_to_leave)

    # ---- leaving ----

    def _ask_to_leave(self) -> None:
        """Send ``leave`` on the session carried now, unless it holds one already, if the
        cloud takes it (platform/protocols/cloud/README.md, leave)."""
        self._asking = None
        leaving, session = self._leaving, self.session
        if leaving is None or leaving.done() or session is None or self.connection is None:
            return
        if session.leave is not None:
            return  # sent, and kept until the cloud receives it, across connections
        if session.protocol < LEAVE_SINCE:
            log.warning("the cloud speaks the link %d.%d, which has no leave", *session.protocol)
            leaving.set_result(False)
            return
        session.leave = session.send({"type": "leave"})

    def _left(self) -> None:
        """The robot is out of its home: the outcome of its leave, if it asked."""
        leaving = self._leaving
        if leaving is not None and not leaving.done():
            log.info("the robot left its home")
            leaving.set_result(True)
            self._hurry.clear()

    # ---- statuses ----

    def _report(self) -> None:
        """Tell the robot the link's status, if it changed."""
        session = self.session
        if session is None or self.connection is None:
            status = LinkStatus(False, bound=self.bound, update_required=self.update_required)
        else:
            status = LinkStatus(
                True,
                session.id,
                session.events,
                conversations=session.conversations,
                bound=self.bound,
                update_required=self.update_required,
            )
        if status != self._reported:
            self._reported = status
            self._statuses.put_nowait(status)

    def _known(self, bound: bool) -> None:
        """The server said whether it knows the robot."""
        if not bound:
            self._left()  # a refused token too ends a leave (leave)
        if bound != self.bound:
            self.bound = bound
            self._report()

    def _needs_update(self, why: str) -> None:
        if not self.update_required:
            log.error("the robot needs an update: %s", why)
            self.update_required = True
            self._report()

    # ---- connecting, again and again ----

    async def _run(self) -> None:
        """Connect, hold the session, and connect again after a loss, for ever."""
        await self._described.wait()
        await self._offered.wait()
        failures = 0  # attempts in a row that did not complete a hello exchange
        reported = False  # whether this outage was logged
        while not self._closing:
            ending: Ending | None = None
            wait = 0.0  # the least the robot waits before the next attempt
            try:
                ending = await self._connect()
            except NotSetUp as error:
                self._known(False)
                reported = _outage(reported, error)
            except Refused as refused:
                if refused.status == 410:
                    self._needs_update("the cloud no longer speaks this version of the link")
                reported = _outage(reported, refused)
                wait = refused.retry_after or 0.0
            except NoToken as error:
                reported = _outage(reported, error)
                wait = error.retry_after or 0.0
            except (aiohttp.ClientError, OSError, TimeoutError) as error:
                reported = _outage(reported, error)
            except Exception:  # the link must outlive any failure, and try again
                log.exception("the cloud link failed")
            if ending is not None:
                wait = max(wait, await self._after(ending))
            if ending is not None and ending.greeted:
                failures, reported = 0, False
            else:
                failures += 1
            longest = self.reconnect_max if self.update_required else None
            await self._pause(wait, self._backoff(failures, longest))

    async def _pause(self, least: float, backoff: float) -> None:
        """Wait before the next attempt: ``backoff``, or only ``least``, the Retry-After
        the robot was given, once it hurries to leave its home (leave)."""
        loop = asyncio.get_running_loop()
        since = loop.time()
        if not self._hurry.is_set():
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(max(least, backoff)):
                    await self._hurry.wait()
        if self._hurry.is_set():
            self._hurry.clear()
            await asyncio.sleep(max(0.0, since + least - loop.time()))

    def _backoff(self, failures: int, longest: float | None = None) -> float:
        """A random wait before the next attempt: exponential backoff with full jitter,
        or the longest one."""
        if longest is None:
            longest = min(self.reconnect_max, self.reconnect_first * 2**failures)
        return random.uniform(0, longest)

    async def _connect(self) -> Ending:
        """One connection: get a token, open the WebSocket, and hold the session on it."""
        if self.identity is None:
            raise NoToken("no identity to prove the robot's to the server")
        assert self._http is not None and self._token_schema is not None
        assert self.schema is not None
        token = await access_token(
            self._http, self.server, self.identity, self._token_schema, self.connect_timeout
        )
        self._known(True)
        try:
            async with asyncio.timeout(self.connect_timeout):
                ws = await self._http.ws_connect(
                    _link_url(self.server),
                    headers={"Authorization": f"Bearer {token}"},
                    # aiohttp refuses an uncompressed message of max_msg_size bytes and
                    # more; the robot offers no compression, as aiohttp's client does not.
                    max_msg_size=MAX_MESSAGE + 1,
                    timeout=aiohttp.ClientWSTimeout(ws_close=self.connect_timeout),
                )
        except aiohttp.WSServerHandshakeError as error:
            raise Refused(error.status, retry_after(error.headers)) from None
        try:
            return await self._hold(Connection(ws, self.schema, self.connect_timeout))
        finally:
            await ws.close()

    async def _hold(self, connection: Connection) -> Ending:
        """Say hello on ``connection``, then carry the session on it until it ends."""
        assert self.device is not None and self.schema is not None
        resuming, said = self.session, self.device
        declared = {kind.name for kind in self.event_kinds}
        offered = self.audio is not None
        await connection.say(self._hello(said, resuming))
        try:
            async with asyncio.timeout(self.hello_timeout):
                answer = await connection.first()
        except TimeoutError:
            log.warning("the cloud did not answer the hello within %g s", self.hello_timeout)
            answer = Ending(greeted=False, reason="no_hello", message=NOT_IN_TIME)
        if isinstance(answer, Ending):
            return await connection.end(answer)
        if answer["resumed"] and (resuming is None or answer["session"] != resuming.id):
            log.warning("the cloud's hello resumes a session the robot did not name")
            return await connection.end(Ending(greeted=False, reason="no_hello"))
        if answer["resumed"]:
            assert resuming is not None
            session = resuming
            session.protocol = version(answer["protocol"])
            unreceived = session.unreceived(answer["received"])
            if unreceived is None:
                log.warning("cannot resume the cloud session %s: too much was lost", session.id)
                await connection.finish("resume_failed")
                return Ending(greeted=True, reason="resume_failed")
            for text in unreceived:
                connection.write(text)
            log.info("cloud session %s resumed", session.id)
        else:
            await self._forget()
            session = Session(self, answer["session"], next(self._numbers), self.schema)
            session.protocol = version(answer["protocol"])
            self.session = session
            log.info("cloud session %s open (protocol %s)", session.id, answer["protocol"])
        session.events = frozenset(answer["events"]) & declared
        session.conversations = offered and answer["conversations"]
        session.connection = self.connection = connection
        changes = _changes(said, self.device)  # what the owner changed since the hello
        if changes:
            session.changed(changes)
        while self._waiting:
            session.emit(*self._waiting.popleft())
        if self.update_required:
            log.info("the cloud accepts the robot's software again")
            self.update_required = False
        self._report()
        self._ask_to_leave()
        try:
            return await connection.carry(session, answer["ping_interval"])
        finally:
            session.connection = self.connection = None
            if session.leave is not None and self._leaving is not None:
                self._hurry.set()  # the leave's answer may be lost with the connection
            self._report()

    def _hello(self, device: DeviceInfo, resuming: Session | None) -> dict[str, Any]:
        """The robot's ``hello``: what it is, what its owner chose, what it can say."""
        hello: dict[str, Any] = {
            "type": "hello",
            "protocol": PROTOCOL,
            "device": {"kind": device.kind, "model": device.model, "software": software_version()},
            "time": instant(time.time()),
            "time_zone": device.time_zone,
            "languages": list(device.languages),
            "allowed_classes": sorted(str(safety) for safety in device.allowed_classes),
            "conversations": self.audio is not None,
            "media": [MEDIA] if self.audio is not None else [],
            "events": [
                {"name": kind.name, "description": kind.description, "schema": plain(kind.schema)}
                for kind in self.event_kinds
            ],
        }
        if resuming is not None:
            hello["resume"] = {"session": resuming.id, "received": resuming.received}
        conversation = self.conversations.named()
        if conversation is not None:
            hello["conversation"] = conversation
        return hello

    async def _after(self, ending: Ending) -> float:
        """Act on how a connection ended. Returns the least wait before the next one."""
        if ending.reason is None:
            return 0.0  # lost: the next connection resumes the session
        await self._forget()
        if not ending.by_cloud:
            return 0.0
        log.warning("the cloud ended the session: %s", ending.reason)
        if ending.reason in ("revoked", "left"):
            self._known(False)
        elif ending.reason == "update_required":
            self._needs_update("the cloud asks for it")
        elif ending.reason == "replaced":
            return self._backoff(0, self.reconnect_max)  # keep to the newer connection
        return 0.0

    async def _forget(self) -> None:
        """The session is over: its calls in progress are cancelled."""
        session, self.session = self.session, None
        if session is None:
            return
        await session.close()
        for call in session.mcp.abandon():
            self._requests.put_nowait(CallCancelled(call))
        self._report()


async def _refuse_redirect(
    session: aiohttp.ClientSession, context: Any, params: aiohttp.TraceRequestRedirectParams
) -> None:
    """A redirect would carry the robot's token where it never chose to send it: the
    robot follows none (SEC-1), and takes one as a refusal."""
    raise Refused(params.response.status, None)


def _count(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a whole number from 1, got {value!r}")
    return value


def _outage(reported: bool, error: BaseException) -> bool:
    """Log a failure to connect once for each outage; returns that it was."""
    if not reported:
        log.warning("cannot reach the cloud: %s", error or type(error).__name__)
    return True


def _load_schemas(path: Path) -> tuple[Schema, Schema, Schema]:
    """From the protocol's schema.json: the schemas of the cloud's messages on the
    control channel, of the agents' in a room, and of the token endpoint's answer."""
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot read the cloud link's schema: {error}") from None
    name = "platform/protocols/cloud/schema.json"
    return (
        Schema(name, {**schema, "$ref": "#/$defs/from_cloud"}),
        Schema(name, {**schema, "$ref": "#/$defs/from_agent"}),
        Schema(name, {**schema, "$ref": "#/$defs/token_response"}),
    )


def _link_url(server: str) -> str:
    """The control channel's address on ``server``: ``wss://``, or ``ws://`` for an
    ``http://`` server."""
    scheme, rest = server.rstrip("/").split("://", 1)
    return f"{'wss' if scheme == 'https' else 'ws'}://{rest}{LINK_PATH}"


def _changes(before: DeviceInfo, after: DeviceInfo) -> dict[str, Any]:
    """What ``device.changed`` says: the owner's choices that differ from ``before``."""
    changes: dict[str, Any] = {}
    if after.languages != before.languages:
        changes["languages"] = list(after.languages)
    if after.time_zone != before.time_zone:
        changes["time_zone"] = after.time_zone
    if after.allowed_classes != before.allowed_classes:
        changes["allowed_classes"] = sorted(str(safety) for safety in after.allowed_classes)
    return changes
