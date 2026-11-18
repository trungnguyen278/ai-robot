# SPDX-License-Identifier: MIT
"""Access tokens for the control channel, from ai-cloud's token endpoint, in exchange for
an assertion signed by the robot's key (platform/protocols/cloud/README.md, Tokens from
the server)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import jwt
import pytest
from fake_cloud import FakeCloud, schema

from ai_robot.adapters.sim.identity import SimIdentity
from ai_robot.adapters.websocket.tokens import (
    MAX_ANSWER,
    NoToken,
    NotSetUp,
    access_token,
    assertion,
)
from ai_robot.core.jose import b64url, thumbprint

TIMEOUT = 5.0
DAY = 86400


@contextlib.asynccontextmanager
async def server_and_robot(
    known: bool = True,
) -> AsyncIterator[tuple[FakeCloud, SimIdentity, aiohttp.ClientSession]]:
    identity = SimIdentity()
    await identity.open()
    robots = {await identity.robot_id(): await identity.public_key()} if known else {}
    server = FakeCloud(robots)
    await server.start()
    try:
        async with aiohttp.ClientSession() as http:
            yield server, identity, http
    finally:
        await server.stop()


async def token(server: FakeCloud, identity: SimIdentity, http: aiohttp.ClientSession) -> str:
    return await access_token(http, server.url, identity, schema("token_response"), TIMEOUT)


def test_a_robot_the_server_knows_gets_its_token() -> None:
    async def main() -> None:
        async with server_and_robot() as (server, identity, http):
            assert await token(server, identity, http) == "access-1"
            robot = await identity.robot_id()
            (claims,) = server.assertions
            assert claims["iss"] == claims["sub"] == robot
            assert claims["aud"] == server.endpoint
            assert claims["exp"] - claims["iat"] == 60
            assert await token(server, identity, http) == "access-2"
            assert server.assertions[0]["jti"] != server.assertions[1]["jti"]

    asyncio.run(main())


def test_the_assertion_is_a_standard_es256_jwt() -> None:
    async def main() -> None:
        identity = SimIdentity()
        await identity.open()
        signed = await assertion(identity, "https://cloud.example/device/token")
        jwk = await identity.public_key()
        header = jwt.get_unverified_header(signed)
        assert header == {"alg": "ES256", "typ": "JWT", "kid": thumbprint(jwk)}
        claims = jwt.decode(
            signed,
            jwt.PyJWK(dict(jwk)).key,
            algorithms=["ES256"],
            audience="https://cloud.example/device/token",
        )
        assert claims["iss"] == await identity.robot_id()

    asyncio.run(main())


def test_the_key_id_is_the_rfc_7638_thumbprint() -> None:
    jwk = {"kty": "EC", "crv": "P-256", "x": "xx", "y": "yy", "use": "sig"}
    canonical = b'{"crv":"P-256","kty":"EC","x":"xx","y":"yy"}'  # required members, in order
    assert thumbprint(jwk) == b64url(hashlib.sha256(canonical).digest())


def test_a_robot_the_server_does_not_know_must_be_set_up() -> None:
    async def main() -> None:
        async with server_and_robot(known=False) as (server, identity, http):
            with pytest.raises(NotSetUp, match="does not know this robot: it must be set up"):
                await token(server, identity, http)

    asyncio.run(main())


LATER = "Wed, 21 Oct 2099 07:28:00 GMT"  # beyond the longest wait the protocol speaks of


@pytest.mark.parametrize(
    ("status", "error", "headers", "said", "wait"),
    [
        (400, "invalid_grant", {}, "400, invalid_grant", None),
        (400, "invalid_request", {}, "400, invalid_request", None),
        (429, "temporarily_unavailable", {"Retry-After": "7"}, "429, temporarily_unavailable", 7),
        (
            503,
            "temporarily_unavailable",
            {"Retry-After": LATER},
            "503, temporarily_unavailable",
            DAY,
        ),
        # Only a 401 with invalid_client says the server does not know the robot (PROV-3).
        (400, "invalid_client", {}, "400, invalid_client", None),
        (401, "invalid_grant", {}, "401, invalid_grant", None),
        (401, "secret-code", {}, "401, another error", None),
    ],
)
def test_a_refused_token_is_no_reason_to_set_up_again(
    status: int, error: str, headers: dict[str, str], said: str, wait: float | None
) -> None:
    async def main() -> None:
        async with server_and_robot() as (server, identity, http):
            server.refuse_token = (status, error, headers)
            with pytest.raises(NoToken) as refused:
                await token(server, identity, http)
            assert type(refused.value) is NoToken
            assert str(refused.value) == f"the server refused the robot's token ({said})"
            assert refused.value.retry_after == wait

    asyncio.run(main())


def test_a_refusal_the_robot_cannot_read_is_no_reason_to_set_up_again() -> None:
    async def main() -> None:
        async with server_and_robot() as (server, identity, http):
            server.refuse_token = (401, "invalid_client\x00", {})  # not JSON every side holds
            with pytest.raises(NoToken) as refused:
                await token(server, identity, http)
            assert type(refused.value) is NoToken
            assert "another error" in str(refused.value)

    asyncio.run(main())


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        ({"access_token": "a\x00", "token_type": "Bearer", "expires_in": 600}, "is not a token"),
        ({"access_token": "a"}, "is not a token"),
        ("not a token", "is not a token"),
        ({"access_token": "a", "token_type": "Bearer", "expires_in": 0}, "is not a token"),
        ({"access_token": "a", "token_type": "MAC", "expires_in": 600}, "uses bearer"),
    ],
)
def test_an_answer_that_is_not_a_proper_token_is_refused(answer: Any, message: str) -> None:
    async def main() -> None:
        async with server_and_robot() as (server, identity, http):
            server.answer = answer
            with pytest.raises(NoToken, match=message):
                await token(server, identity, http)

    asyncio.run(main())


def padded(answer: dict[str, Any], size: int) -> bytes:
    """``answer`` in JSON, followed by spaces that make it ``size`` bytes long: what the
    robot would take if it read only part of it."""
    text = json.dumps(answer).encode()
    return text + b" " * (size - len(text))


@pytest.mark.parametrize(("size", "taken"), [(MAX_ANSWER, True), (MAX_ANSWER + 1, False)])
def test_an_answer_longer_than_the_robot_reads_is_no_token(size: int, taken: bool) -> None:
    async def main() -> None:
        async with server_and_robot() as (server, identity, http):
            answer = {"access_token": "a", "token_type": "Bearer", "expires_in": 600}
            server.answer = padded(answer, size)
            if taken:
                assert await token(server, identity, http) == "a"
                return
            with pytest.raises(NoToken, match="is not a token: must be of type object"):
                await token(server, identity, http)

    asyncio.run(main())


def test_a_refusal_longer_than_the_robot_reads_still_says_when_to_ask_again() -> None:
    async def main() -> None:
        async with server_and_robot() as (server, identity, http):
            server.refuse_token = (429, "x" * MAX_ANSWER, {"Retry-After": "7"})
            with pytest.raises(NoToken) as refused:
                await token(server, identity, http)
            assert str(refused.value) == "the server refused the robot's token (429, another error)"
            assert refused.value.retry_after == 7

    asyncio.run(main())


def test_a_bearer_token_is_taken_whatever_its_case() -> None:
    async def main() -> None:
        async with server_and_robot() as (server, identity, http):
            server.answer = {"access_token": "a", "token_type": "bearer", "expires_in": 600}
            assert await token(server, identity, http) == "a"

    asyncio.run(main())


def test_the_robot_does_not_follow_a_redirect() -> None:
    async def main() -> None:
        async with server_and_robot() as (server, identity, http):
            server.redirect = server.url + "/elsewhere"
            with pytest.raises(NoToken, match="redirects"):
                await token(server, identity, http)
            assert server.elsewhere == []  # the assertion went nowhere else

    asyncio.run(main())
