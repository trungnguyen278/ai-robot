# SPDX-License-Identifier: MIT
"""ai-cloud as the tests need it (platform/protocols/cloud/README.md): its token
endpoint, and its side of the control channel, which a test plays.

The token endpoint checks assertions with PyJWT, a JWT library of its own, so that the
robot's hand-made ES256 assertions are proven to be standard. Every message the robot
sends on the control channel is read as the cloud link 3.1 reads it, JSON that every
side can hold, and checked against the protocol's schema, and its numbering; what breaks
any of them is kept in ``problems``.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import jwt
from aiohttp import WSMsgType, web

from ai_robot.core.data import Schema, read_json
from ai_robot.core.jose import thumbprint

ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
SCHEMA = Path(__file__).resolve().parents[2] / "platform" / "protocols" / "cloud" / "schema.json"
UNNUMBERED = {"hello", "ping", "pong", "reconnect", "bye"}
MAX_MESSAGE = 1 << 20  # bytes in a message, past which a side closes with 1009
TIMEOUT = 5.0


def schema(name: str) -> Schema:
    """The protocol's schema of ``$defs/<name>``."""
    root = json.loads(SCHEMA.read_text(encoding="utf-8"))
    return Schema(name, {**root, "$ref": f"#/$defs/{name}"})


class CloudSide:
    """The cloud's side of one connection of the control channel.

    It reads what the robot sends: pings are answered at once (unless ``answer_pings``
    is off) and kept in ``pings``; ``next`` gives the other messages, in order.
    ``received`` is the number of the last one the cloud received, and ``sent`` that
    of the last it sent. ``close_code`` is how the connection closed, once it did.
    """

    def __init__(self, ws: web.WebSocketResponse, problems: list[str]) -> None:
        self.ws = ws
        self.problems = problems
        self.received = 0
        self.sent = 0
        self.answer_pings = True
        self.pings: list[dict[str, Any]] = []
        self.closed = asyncio.Event()
        self.close_code: int | None = None
        self._messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._schema = schema("from_device")

    async def run(self) -> None:
        try:
            async for frame in self.ws:
                if frame.type is not WSMsgType.TEXT:
                    self.problems.append(f"a {frame.type.name} frame")
                    continue
                try:
                    message = read_json(frame.data)
                except ValueError as error:
                    self.problems.append(f"a text that the cloud cannot read: {error}")
                    continue
                await self._take(message)
        finally:
            self.close_code = self.ws.close_code
            self.closed.set()

    async def _take(self, message: dict[str, Any]) -> None:
        problem = self._schema.problem(message)
        if problem is not None:
            self.problems.append(f"{message}: {problem}")
        if message.get("type") not in UNNUMBERED:
            if message.get("seq") != self.received + 1:
                self.problems.append(f"{message} comes after {self.received}")
            self.received = message.get("seq", self.received)
        if message.get("type") == "ping":
            self.pings.append(message)
            if self.answer_pings:
                await self.send({"type": "pong", "received": self.received})
            return
        self._messages.put_nowait(message)

    async def next(self, kind: str | None = None, timeout: float = TIMEOUT) -> dict[str, Any]:
        """The robot's next message (pings aside), which must be of type ``kind``."""
        message = await asyncio.wait_for(self._messages.get(), timeout)
        if kind is not None:
            assert message["type"] == kind, message
        return message

    def pending(self) -> list[dict[str, Any]]:
        """What the robot sent that no ``next`` has taken yet."""
        messages = []
        while not self._messages.empty():
            messages.append(self._messages.get_nowait())
        return messages

    async def send(self, message: Mapping[str, Any]) -> dict[str, Any]:
        """Send ``message``, numbered unless it says otherwise or is of a kind that has
        no number."""
        message = dict(message)
        if message["type"] not in UNNUMBERED and "seq" not in message:
            self.sent += 1
            message = {"type": message["type"], "seq": self.sent, **message}
        await self.ws.send_str(json.dumps(message))
        return message

    async def hello(
        self,
        session: str = "01a18085-942c-78a0-9282-31c8cede2442",
        *,
        resumed: bool = False,
        received: int | None = None,
        ping_interval: float = 30.0,
        events: tuple[str, ...] = (),
        sent: int = 0,
        **more: Any,
    ) -> dict[str, Any]:
        """Answer the robot's hello: a new session, or ``session`` resumed, having
        received up to ``received``. ``sent`` is the number of the cloud's last message
        before this connection, when it resumes a session."""
        self.sent = sent
        hello: dict[str, Any] = {
            "type": "hello",
            "protocol": "3.3",
            "session": session,
            "resumed": resumed,
            "time": "2026-10-28T03:20:00.310Z",
            "ping_interval": ping_interval,
            "conversations": False,
            "events": list(events),
            **more,
        }
        if received is not None:
            hello["received"] = received
            self.received = received
        return await self.send(hello)

    async def mcp(
        self, method: str, params: Mapping[str, Any] | None = None, *, request: Any = 1
    ) -> dict[str, Any]:
        """Send the MCP request ``request``, with the metadata MCP requires."""
        meta = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
        }
        payload = {"jsonrpc": "2.0", "id": request, "method": method, "params": {"_meta": meta}}
        payload["params"].update(params or {})
        return await self.send({"type": "mcp", "payload": payload})

    async def close(self) -> None:
        await self.ws.close()
        await asyncio.wait_for(self.closed.wait(), TIMEOUT)


class FakeCloud:
    """Knows the robots in ``robots`` (identifier -> public JWK). Each connection to its
    control channel waits in ``connections`` for a test to play the cloud's side.

    - ``answer``, if set, is what the token endpoint answers instead of a token, as JSON
      or as the bytes it is, and
      ``redirect`` where it redirects to; ``refuse_token``, if set, is the refusal it
      answers to a robot that it knows: a status, an error and headers, and
      ``token_refusals`` counts them;
    - ``redirect_link``, if set, is where the control channel redirects to;
    - ``refuse``, if set, is the status that the control channel's handshake answers,
      with ``refuse_headers``; ``refuse_tokens`` handshakes are refused as if their
      token were not valid;
    - ``assertions`` keeps every assertion the token endpoint accepted.
    """

    def __init__(self, robots: Mapping[str, Mapping[str, str]]) -> None:
        self.robots = dict(robots)
        self.answer: Any = None
        self.redirect: str | None = None
        self.refuse_token: tuple[int, str, dict[str, str]] | None = None
        self.token_refusals = 0
        self.redirect_link: str | None = None
        self.elsewhere: list[str] = []  # what reached /elsewhere, which a redirect may name
        self.refuse: int | None = None
        self.refuse_headers: dict[str, str] = {}
        self.refuse_tokens = 0
        self.refusals = 0  # handshakes it refused
        self.assertions: list[dict[str, Any]] = []
        self.tokens: set[str] = set()  # the access tokens it gave
        self.bearers: list[str] = []  # the token each connection brought
        self.problems: list[str] = []
        self.connections: asyncio.Queue[CloudSide] = asyncio.Queue()
        self.url = ""
        self._numbers = itertools.count(1)
        self._runner: web.AppRunner | None = None

    @property
    def endpoint(self) -> str:
        return self.url + "/device/token"

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/device/token", self._token)
        app.router.add_post("/elsewhere", self._elsewhere)
        app.router.add_get("/elsewhere", self._elsewhere)
        app.router.add_get("/device/v3/link", self._link)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", 0).start()
        self.url = f"http://127.0.0.1:{self._runner.addresses[0][1]}"

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def connection(self, timeout: float = TIMEOUT) -> CloudSide:
        """The next connection of the robot."""
        return await asyncio.wait_for(self.connections.get(), timeout)

    async def _elsewhere(self, request: web.Request) -> web.Response:
        self.elsewhere.append(await request.text())
        return web.json_response(self.answer or {})

    async def _token(self, request: web.Request) -> web.Response:
        if self.redirect is not None:
            raise web.HTTPTemporaryRedirect(self.redirect)  # 307: a client would resend the body
        form = await request.post()
        if (
            form.get("grant_type") != "client_credentials"
            or form.get("client_assertion_type") != ASSERTION_TYPE
        ):
            return refusal(400, "invalid_request")
        assertion = str(form.get("client_assertion", ""))
        try:
            header = jwt.get_unverified_header(assertion)
            robot = jwt.decode(assertion, options={"verify_signature": False})["iss"]
        except (jwt.PyJWTError, KeyError):
            return refusal(400, "invalid_request")  # no JWT
        key = self.robots.get(robot)
        if key is None or header.get("kid") != thumbprint(key):
            return refusal(401, "invalid_client")  # a key it does not know
        if self.refuse_token is not None:
            self.token_refusals += 1
            status, error, headers = self.refuse_token
            return refusal(status, error, headers)
        try:
            if header.get("typ") != "JWT":
                raise jwt.InvalidTokenError("not a JWT")
            claims = jwt.decode(
                assertion,
                jwt.PyJWK(dict(key)).key,
                algorithms=["ES256"],
                audience=self.endpoint,
                options={"require": ["iss", "sub", "aud", "iat", "exp", "jti"]},
            )
        except jwt.PyJWTError:
            return refusal(400, "invalid_grant")  # the assertion of a key it knows fails
        seen = {old["jti"] for old in self.assertions}
        if claims["sub"] != robot or claims["exp"] - claims["iat"] > 300 or claims["jti"] in seen:
            return refusal(400, "invalid_grant")
        self.assertions.append(claims)
        if isinstance(self.answer, bytes):
            return web.Response(body=self.answer, content_type="application/json")
        if self.answer is not None:
            return web.json_response(self.answer)
        token = f"access-{next(self._numbers)}"
        self.tokens.add(token)
        return web.json_response({"access_token": token, "token_type": "Bearer", "expires_in": 600})

    async def _link(self, request: web.Request) -> web.StreamResponse:
        if self.redirect_link is not None:
            self.refusals += 1
            raise web.HTTPTemporaryRedirect(self.redirect_link)
        if self.refuse is not None:
            self.refusals += 1
            problem = {"type": "about:blank", "title": "Refused", "status": self.refuse}
            return web.json_response(
                problem,
                status=self.refuse,
                headers=self.refuse_headers,
                content_type="application/problem+json",
            )
        bearer = request.headers.get("Authorization", "")
        self.bearers.append(bearer)
        if bearer.removeprefix("Bearer ") not in self.tokens or self.refuse_tokens:
            self.refuse_tokens = max(0, self.refuse_tokens - 1)
            return web.json_response({"title": "Unauthorized"}, status=401)
        # aiohttp refuses a message of max_msg_size bytes and more.
        ws = web.WebSocketResponse(max_msg_size=MAX_MESSAGE + 1)
        await ws.prepare(request)
        side = CloudSide(ws, self.problems)
        self.connections.put_nowait(side)
        with contextlib.suppress(ConnectionError):
            await side.run()
        return ws


def refusal(status: int, error: str, headers: Mapping[str, str] | None = None) -> web.Response:
    """An OAuth 2.0 error (RFC 6749, §5.2)."""
    body = {"error": error, "error_description": error}
    return web.json_response(body, status=status, headers=headers)
