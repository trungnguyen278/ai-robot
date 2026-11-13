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
from ai_robot.core.data import JSON_DEPTH, Schema
from ai_robot.core.jose import b64url, b64url_decode
from ai_robot.core.setup_session import (
    MAX_ERROR,
    MAX_MESSAGE,
    AppSession,
    Ended,
    Messages,
    Refusal,
    Secret,
    Session,
    SetupSchema,
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


def schema() -> SetupSchema:
    root = json.loads((platform_dir() / "setup" / "schema.json").read_text(encoding="utf-8"))
    return SetupSchema(root)


@pytest.fixture(scope="module")
def secret() -> Secret:
    return asyncio.run(new_secret())


def robot(secret: Secret) -> Session:
    return Session(secret, DISCRIMINATOR, schema())


def refused(
    reader: Messages, data: bytes, app: AppSession | None = None
) -> tuple[Refusal, dict[str, Any]]:
    """What the robot refuses ``data`` with, and the error it then sends, read as ``app``
    reads it once encrypted."""
    with pytest.raises(Refusal) as refusal:
        if isinstance(reader, Session):
            reader.receive(data)
        else:
            reader.read(data)
    error = reader.error(refusal.value)
    return refusal.value, json.loads(error) if app is None else app.read(error)


def nested(levels: int) -> Any:
    """``levels`` arrays, each in the next."""
    value: Any = 0
    for _ in range(levels):
        value = [value]
    return value


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
        "protocol": "2.2",
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
    refusal, error = refused(session, confirm.encode())
    assert (refusal.code, refusal.fatal) == ("code", True)
    assert (error["seq"], error["reply_to"]) == (3, 3)  # after its hello and its share


def test_another_major_version_is_refused(secret: Secret) -> None:
    refusal, error = refused(robot(secret), AppSession(secret.code).hello(protocol="1.0"))
    assert (refusal.code, refusal.fatal) == ("version", True)
    assert (error["seq"], error["reply_to"]) == (1, 1)


def test_a_newer_minor_version_is_welcome(secret: Secret) -> None:
    hello, _ = robot(secret).receive(AppSession(secret.code).hello(protocol="2.7"))
    assert hello is not None and json.loads(hello)["protocol"] == "2.2"


@pytest.mark.parametrize(
    "message",
    [
        json.dumps({"type": "hello", "seq": 1}).encode(),  # no protocol
        json.dumps({"type": "share", "seq": 1, "share": b64url(bytes(65))}).encode(),
        json.dumps({"type": "info", "seq": 1}).encode(),  # a request, in the clear
        b'{"type": "hello", "seq": 1.0}',  # 1.0 is an integer to JSON Schema
    ],
)
def test_a_first_message_that_is_not_a_hello_ends_the_session(
    secret: Secret, message: bytes
) -> None:
    refusal, error = refused(robot(secret), message)
    assert (refusal.code, refusal.fatal) == ("protocol", True)
    assert error["reply_to"] == 1 and type(error["reply_to"]) is int


HELLO = {"type": "hello", "seq": 1, "protocol": "2.1"}


@pytest.mark.parametrize(
    "message",
    [
        b"not json",
        b"\xff",  # not UTF-8
        b'["hello"]',
        b'"hello"',
        json.dumps({"type": "hello", "protocol": "2.1"}).encode(),
        *(json.dumps({**HELLO, "seq": seq}).encode() for seq in (0, -1, "1", True, None, 2**53)),
        b'{"type": "hello", "seq": NaN, "protocol": "2.1"}',
        # What ai-platform's ADR-0014 has every side refuse, a seq or not.
        json.dumps({**HELLO, "protocol": chr(0)}).encode(),
        json.dumps({**HELLO, "protocol": chr(0xD800)}).encode(),
        json.dumps({**HELLO, chr(0xDFFF): 1}).encode(),
        json.dumps({**HELLO, "x": nested(JSON_DEPTH)}).encode(),
        b'{"type": "hello", "seq": 1, "protocol": "2.1", "x": 1e400}',
    ],
)
def test_a_message_that_cannot_be_read_is_answered_with_nothing_to_reply_to(
    secret: Secret, message: bytes
) -> None:
    for reader in (robot(secret), Messages(schema())):
        refusal, error = refused(reader, message)
        assert (refusal.code, refusal.fatal) == ("protocol", True)
        assert "reply_to" not in error and error["seq"] == 1


def test_each_message_is_answered_for_itself(secret: Secret) -> None:
    for reader in (robot(secret), Messages(schema())):
        reader.read(json.dumps(HELLO).encode())
        _, error = refused(reader, b"not json")
        assert "reply_to" not in error


def test_json_as_deep_as_every_side_reads_is_read(secret: Secret) -> None:
    deepest = json.dumps({**HELLO, "x": nested(JSON_DEPTH - 1)})  # the message is one
    hello, _ = robot(secret).receive(deepest.encode())
    assert hello is not None


def test_a_share_that_is_not_a_point_ends_the_session(secret: Secret) -> None:
    session, app = robot(secret), AppSession(secret.code)
    session.receive(app.hello())
    share = {"type": "share", "seq": 2, "share": b64url(b"\x04" + bytes(64))}
    refusal, error = refused(session, json.dumps(share).encode())
    assert "share" in refusal.message and refusal.fatal and error["reply_to"] == 2


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
    refusal, error = refused(session, json.dumps({"type": "info", "seq": 9}).encode(), app)
    assert "decrypt" in refusal.message and "reply_to" not in error


def test_an_encrypted_message_that_breaks_the_schema_is_answered(secret: Secret) -> None:
    session, app = robot(secret), AppSession(secret.code)
    exchange(session, app)
    refusal, error = refused(session, app.request("join", password="x"), app)  # no name
    assert refusal.code == "protocol" and error["reply_to"] == 4


def test_a_message_too_long_ends_the_session(secret: Secret) -> None:
    refusal, error = refused(robot(secret), b" " * (MAX_MESSAGE + 1))
    assert "too long" in refusal.message and "reply_to" not in error


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
        # No side holds a U+0000 in a string (ADR-0014), and a name from the air may hold
        # one: hostapd hides a network behind NUL bytes.
        ({"ssid_hex": "00000000"}, bytes(4)),
        ({"ssid_hex": "480069"}, b"H" + bytes(1) + b"i"),
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
