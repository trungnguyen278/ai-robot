# SPDX-License-Identifier: MIT
"""Access tokens for the control channel (platform/protocols/cloud/README.md, Tokens
from the server).

The robot gets one from ai-cloud's token endpoint before each connection, with an
assertion signed by its key (docs/decisions/0010-device-identity.md): OAuth 2.0 client
authentication with a JWT (RFC 7523, §2.2).
"""

from __future__ import annotations

import secrets
import time

import aiohttp

from ai_robot.adapters.websocket.retry import retry_after
from ai_robot.core.data import Schema, read_json
from ai_robot.core.jose import b64url, b64url_json, thumbprint
from ai_robot.ports.identity import Identity

# Fixed by the protocol (platform/protocols/cloud/README.md, Tokens from the server).
TOKEN_PATH = "/device/token"
GRANT_TYPE = "client_credentials"
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"  # RFC 7523, §2.2
TOKEN_TYPE = "bearer"  # the type the robot uses; OAuth 2.0 compares it without case
NOT_SET_UP = (401, "invalid_client")  # a key the server does not know, or revoked
# The refusals the protocol names, which a log may name too: no other text from the server.
REFUSALS = frozenset(
    {"invalid_client", "invalid_grant", "invalid_request", "temporarily_unavailable"}
)

# How this implementation works, which nobody needs to tune.
ASSERTION_SECONDS = 60  # how long an assertion is valid; the protocol allows up to 300
# The most of an answer the robot reads, as golang.org/x/oauth2 does (internal/token.go,
# 2026-11-06): a token takes a few kilobytes, and a larger answer is unreadable.
MAX_ANSWER = 1 << 20  # bytes


class NoToken(Exception):
    """The robot could not get an access token. ``retry_after``, in seconds, is how long
    the server asked it to wait before it asks again, if it did."""

    def __init__(self, text: str, retry_after: float | None = None) -> None:
        super().__init__(text)
        self.retry_after = retry_after


class NotSetUp(NoToken):
    """The server does not know the robot's key, or revoked it: the robot is not set up."""


async def assertion(identity: Identity, audience: str) -> str:
    """A JWT that proves the robot's identity to ``audience``, signed with ES256 by its
    key (RFC 7523, §3)."""
    robot = await identity.robot_id()
    now = int(time.time())
    header = {"alg": "ES256", "typ": "JWT", "kid": thumbprint(await identity.public_key())}
    claims = {
        "iss": robot,
        "sub": robot,
        "aud": audience,
        "iat": now,
        "exp": now + ASSERTION_SECONDS,
        "jti": secrets.token_urlsafe(16),
    }
    signed = f"{b64url_json(header)}.{b64url_json(claims)}"
    return f"{signed}.{b64url(await identity.sign(signed.encode('ascii')))}"


async def access_token(
    http: aiohttp.ClientSession,
    server: str,
    identity: Identity,
    schema: Schema,
    timeout: float,
) -> str:
    """Ask the token endpoint of ``server`` for an access token, within ``timeout``
    seconds."""
    endpoint = server.rstrip("/") + TOKEN_PATH
    form = {
        "grant_type": GRANT_TYPE,
        "client_assertion_type": ASSERTION_TYPE,
        "client_assertion": await assertion(identity, endpoint),
    }
    # A redirect would carry the assertion somewhere the robot never chose: refuse it.
    async with http.post(
        endpoint,
        data=form,
        allow_redirects=False,
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as response:
        if 300 <= response.status < 400:
            raise NoToken(
                f"the token endpoint redirects ({response.status}); the robot does not follow"
            )
        try:
            answer = read_json(await _body(response))  # JSON that every side can hold
        except ValueError:
            answer = None
        if response.status != 200:
            error = answer.get("error") if isinstance(answer, dict) else None
            if (response.status, error) == NOT_SET_UP:
                raise NotSetUp("the server does not know this robot: it must be set up")
            said = error if isinstance(error, str) and error in REFUSALS else "another error"
            raise NoToken(
                f"the server refused the robot's token ({response.status}, {said})",
                retry_after(response.headers),
            )
    problem = schema.problem(answer)
    if problem is not None:
        raise NoToken(f"the server's answer is not a token: {problem}")
    if answer["token_type"].lower() != TOKEN_TYPE:
        raise NoToken("the server gave a token of another type; the robot uses bearer")
    token: str = answer["access_token"]
    return token


async def _body(response: aiohttp.ClientResponse) -> bytes:
    """The body of ``response``, unless it is longer than ``MAX_ANSWER``: ``ValueError``
    then, without reading the rest."""
    body = bytearray()
    while len(body) <= MAX_ANSWER:
        chunk = await response.content.read(MAX_ANSWER + 1 - len(body))
        if not chunk:
            return bytes(body)
        body += chunk
    raise ValueError(f"the answer is longer than {MAX_ANSWER} bytes")
