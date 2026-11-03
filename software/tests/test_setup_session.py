# SPDX-License-Identifier: MIT
"""The set-up protocol's session: the exchange from the code on the screen, the encrypted
messages, and what the robot refuses."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import pytest

from ai_robot.core.config import platform_dir
from ai_robot.core.data import Schema
from ai_robot.core.jose import b64url, b64url_decode
from ai_robot.core.setup_session import (
    MAX_ERROR,
    MAX_MESSAGE,
    AppSession,
    Ended,
    Refusal,
    Secret,
    Session,
    Stage,
    draw_code,
    draw_discriminator,
    new_secret,
    setup_string,
    ssid_bytes,
    ssid_fields,
)
from ai_robot.core.spake2plus import ExchangeError

DISCRIMINATOR = "3F7A"


def schema() -> Schema:
    root = json.loads((platform_dir() / "setup" / "schema.json").read_text(encoding="utf-8"))
    return Schema("platform/protocols/setup/schema.json", {**root, "$ref": "#/$defs/from_app"})


@pytest.fixture(scope="module")
def secret() -> Secret:
    return asyncio.run(new_secret())


def robot(secret: Secret) -> Session:
    return Session(secret, DISCRIMINATOR, schema())


def exchange(session: Session, app: AppSession) -> None:
    """Run the exchange to its end."""
    hello, _ = session.receive(app.hello())
    assert hello is not None
    share, _ = session.receive(app.share(hello))
    assert share is not None
    assert session.receive(app.confirm(share)) == (None, None)


def test_the_exchange_opens_an_encrypted_session(secret: Secret) -> None:
    session, app = robot(secret), AppSession(secret.code)
    exchange(session, app)
    assert session.stage is Stage.OPEN and app.discriminator == DISCRIMINATOR
    sealed = app.request("info")
    assert b"info" not in sealed  # encrypted
    _, request = session.receive(sealed)
    assert request == {"type": "info", "seq": 4}
    answer = session.pack(session.message("finished", request["seq"]))
    assert app.read(answer) == {"type": "finished", "seq": 3, "reply_to": 4}


def test_the_robots_hello_gives_its_discriminator_and_the_salt(secret: Secret) -> None:
    hello, _ = robot(secret).receive(AppSession(secret.code).hello())
    assert hello is not None
    message = json.loads(hello)
    assert message == {
        "type": "hello",
        "seq": 1,
        "reply_to": 1,
        "protocol": "2.0",
        "discriminator": DISCRIMINATOR,
        "salt": b64url(secret.salt),
    }


def test_a_wrong_code_fails_on_both_sides(secret: Secret) -> None:
    wrong = f"{(int(secret.code) + 1) % 10**8:08d}"
    session, app = robot(secret), AppSession(wrong)
    hello, _ = session.receive(app.hello())
    assert hello is not None
    share, _ = session.receive(app.share(hello))
    assert share is not None and session.attempted
    with pytest.raises(ExchangeError):  # the app sees it first, from the robot's confirm
        app.confirm(share)
    # An app that confirmed anyway is refused, and the session ends.
    confirm = json.dumps({"type": "confirm", "seq": 3, "confirm": b64url(bytes(32))})
    with pytest.raises(Refusal) as refusal:
        session.receive(confirm.encode())
    assert (refusal.value.code, refusal.value.fatal) == ("code", True)


def test_another_major_version_is_refused(secret: Secret) -> None:
    with pytest.raises(Refusal) as refusal:
        robot(secret).receive(AppSession(secret.code).hello(protocol="1.0"))
    assert (refusal.value.code, refusal.value.fatal) == ("version", True)


def test_a_newer_minor_version_is_welcome(secret: Secret) -> None:
    hello, _ = robot(secret).receive(AppSession(secret.code).hello(protocol="2.7"))
    assert hello is not None and json.loads(hello)["protocol"] == "2.0"


@pytest.mark.parametrize(
    "message",
    [
        b"not json",
        b'["hello"]',
        json.dumps({"type": "hello", "seq": 1}).encode(),  # no protocol
        json.dumps({"type": "share", "seq": 1, "share": b64url(bytes(65))}).encode(),
        json.dumps({"type": "info", "seq": 1}).encode(),  # a request, in the clear
    ],
)
def test_a_first_message_that_is_not_a_hello_ends_the_session(
    secret: Secret, message: bytes
) -> None:
    with pytest.raises(Refusal) as refusal:
        robot(secret).receive(message)
    assert (refusal.value.code, refusal.value.fatal) == ("protocol", True)


def test_a_share_that_is_not_a_point_ends_the_session(secret: Secret) -> None:
    session, app = robot(secret), AppSession(secret.code)
    session.receive(app.hello())
    share = {"type": "share", "seq": 2, "share": b64url(b"\x04" + bytes(64))}
    with pytest.raises(Refusal, match="share") as refusal:
        session.receive(json.dumps(share).encode())
    assert refusal.value.fatal


def test_an_app_that_ends_the_session_gets_no_answer(secret: Secret) -> None:
    error = {"type": "error", "seq": 1, "code": "code", "message": "the robot's code is wrong"}
    with pytest.raises(Ended):
        robot(secret).receive(json.dumps(error).encode())


def test_encrypted_messages_cannot_be_changed_or_played_again(secret: Secret) -> None:
    session, app = robot(secret), AppSession(secret.code)
    exchange(session, app)
    first = app.request("info")
    tampered = bytes([first[0] ^ 1]) + first[1:]
    with pytest.raises(Refusal, match="decrypt"):
        session.receive(tampered)
    session, app = robot(secret), AppSession(secret.code)
    exchange(session, app)
    first = app.request("info")
    session.receive(first)
    with pytest.raises(Refusal, match="decrypt"):  # the count moved on
        session.receive(first)


def test_messages_in_the_clear_are_refused_once_encrypted(secret: Secret) -> None:
    session, app = robot(secret), AppSession(secret.code)
    exchange(session, app)
    with pytest.raises(Refusal, match="decrypt"):
        session.receive(json.dumps({"type": "info", "seq": 9}).encode())


def test_a_message_too_long_ends_the_session(secret: Secret) -> None:
    with pytest.raises(Refusal, match="too long"):
        robot(secret).receive(b" " * (MAX_MESSAGE + 1))


def test_an_error_s_text_fits_the_protocol(secret: Secret) -> None:
    error = json.loads(robot(secret).error(Refusal("protocol", "x" * (MAX_ERROR + 50))))
    assert len(error["message"]) == MAX_ERROR
    root = json.loads((platform_dir() / "setup" / "schema.json").read_text(encoding="utf-8"))
    device = Schema("setup", {**root, "$ref": "#/$defs/from_device"})
    assert device.problem(error) is None


def test_the_code_stays_out_of_logs(secret: Secret) -> None:
    assert secret.code not in repr(secret) and secret.code not in str(secret)


def test_codes_and_discriminators() -> None:
    assert all(re.fullmatch(r"[0-9]{8}", draw_code()) for _ in range(100))
    assert all(re.fullmatch(r"[0-9A-F]{4}", draw_discriminator()) for _ in range(100))
    assert setup_string("3F7A", "00012345") == "setup:2:3F7A:00012345"


@pytest.mark.parametrize(
    ("fields", "name"),
    [
        ({"ssid": "Home"}, b"Home"),
        ({"ssid": "Nhà của Trung"}, "Nhà của Trung".encode()),
        ({"ssid_hex": "ff00"}, b"\xff\x00"),
    ],
)
def test_network_names(fields: dict[str, Any], name: bytes) -> None:
    assert ssid_bytes(fields) == name
    assert ssid_fields(name) == fields


def test_a_network_name_has_at_most_32_bytes() -> None:
    assert ssid_bytes({"ssid": "a" * 32}) == b"a" * 32
    with pytest.raises(Refusal, match="32 bytes"):
        ssid_bytes({"ssid": "ồ" * 11})  # 11 characters, 33 bytes


@pytest.mark.parametrize("text", ["", "AQ", "AQID", "_-_-"])
def test_base64url_round_trips(text: str) -> None:
    assert b64url(b64url_decode(text)) == text


@pytest.mark.parametrize("text", ["AQ==", "A", "AQ+/", "AQ!D", "AR", "ộ"])
def test_base64url_that_is_not_strict_is_refused(text: str) -> None:
    with pytest.raises(ValueError):
        b64url_decode(text)
