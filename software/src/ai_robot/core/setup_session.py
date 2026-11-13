# SPDX-License-Identifier: MIT
"""The set-up protocol's session (platform/protocols/setup/README.md): the exchange
that turns the code on the robot's screen into keys, then the messages, encrypted.

The transport carries whole messages: it splits and joins them itself (the Bluetooth
fragments), so this module sees only messages. ``Session`` is the robot's side;
``AppSession`` is the app's, for the tests and for tools that play the app.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any

from Crypto.PublicKey.ECC import EccPoint
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ai_robot.core.data import Schema, encode, read_json
from ai_robot.core.jose import b64url, b64url_decode
from ai_robot.core.spake2plus import ExchangeError, Prover, Verifier, record, stretch

# Fixed by the protocol (platform/protocols/setup/README.md).
PROTOCOL = "2.1"
CONTEXT_LABEL = b"ai-platform/setup/2"  # starts the context of the exchange
KEYS_INFO = b"ai-platform/setup/2 keys"  # HKDF's info for the session keys
KEY_BYTES = 16  # each side's AES-128-GCM key
NONCE_ZEROS = bytes(4)  # a nonce is these, then the sender's counter
COUNTER_BYTES = 8
TAG_BYTES = 16  # AES-GCM's tag
MAX_MESSAGE = 8192  # bytes in a message, as sent
MAX_ERROR = 256  # characters in an error's message (schema.json, $defs/error)
SALT_BYTES = 16
CODE_DIGITS = 8
DISCRIMINATOR_DIGITS = 4  # hexadecimal
SSID_BYTES = 32  # the longest network name (IEEE 802.11)


class Refusal(Exception):
    """What the session answers with an error: ``code`` and ``message`` (the protocol's
    Errors). ``fatal`` ends the session."""

    def __init__(self, code: str, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fatal = fatal


class Ended(Exception):
    """The app ended the session, with an error of its own: nothing to answer."""


def draw_code() -> str:
    """A set-up code: 8 decimal digits, drawn uniformly."""
    return f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"


def draw_discriminator() -> str:
    """A discriminator: 4 hexadecimal digits, in capitals."""
    return f"{secrets.randbelow(16**DISCRIMINATOR_DIGITS):0{DISCRIMINATOR_DIGITS}X}"


def setup_string(discriminator: str, code: str) -> str:
    """What the QR code holds: ``setup:<major version>:<discriminator>:<code>``."""
    return f"setup:{PROTOCOL.split('.')[0]}:{discriminator}:{code}"


@dataclass(frozen=True, slots=True)
class Secret:
    """One code, and what the robot keeps of it for the exchange: its salt, w0 and L.
    ``code`` is shown on the screen; it is never logged."""

    code: str
    salt: bytes
    w0: int
    record: EccPoint

    def __repr__(self) -> str:  # keep the code out of logs and tracebacks
        return "Secret(...)"


async def new_secret() -> Secret:
    """A new code, with a new salt. Stretching the code takes a fraction of a second, in a
    thread."""
    code, salt = draw_code(), secrets.token_bytes(SALT_BYTES)
    w0, w1 = await asyncio.to_thread(stretch, code.encode("ascii"), salt)
    return Secret(code, salt, w0, record(w1))


def _context(app_hello: bytes, robot_hello: bytes) -> bytes:
    return hashlib.sha256(CONTEXT_LABEL + app_hello + robot_hello).digest()


def _session_keys(shared: bytes) -> tuple[bytes, bytes]:
    """The app's key and the robot's, from K_shared."""
    kdf = HKDF(algorithm=hashes.SHA256(), length=2 * KEY_BYTES, salt=None, info=KEYS_INFO)
    keys = kdf.derive(shared)
    return keys[:KEY_BYTES], keys[KEY_BYTES:]


class Records:
    """The encrypted messages: AES-128-GCM, each side with its own key, and the sender's
    count of its messages as the nonce."""

    def __init__(self, send_key: bytes, receive_key: bytes) -> None:
        self._send = AESGCM(send_key)
        self._receive = AESGCM(receive_key)
        self._sent = 0
        self._received = 0

    @staticmethod
    def _nonce(count: int) -> bytes:
        return NONCE_ZEROS + count.to_bytes(COUNTER_BYTES, "big")

    def seal(self, plaintext: bytes) -> bytes:
        sealed = self._send.encrypt(self._nonce(self._sent), plaintext, None)
        self._sent += 1
        return sealed

    def open(self, sealed: bytes) -> bytes:
        """The plaintext of the next message; ``Refusal`` if it does not decrypt."""
        try:
            plaintext = self._receive.decrypt(self._nonce(self._received), sealed, None)
        except InvalidTag:
            raise Refusal("protocol", "a message did not decrypt", fatal=True) from None
        self._received += 1
        return plaintext


class SetupSchema:
    """Set-up's schema, as the robot reads the app's messages with it: whole, and a
    message's number alone, which an error can answer only once it can be read."""

    NAME = "platform/protocols/setup/schema.json"

    def __init__(self, root: Mapping[str, Any]) -> None:
        self.from_app = Schema(self.NAME, {**root, "$ref": "#/$defs/from_app"})
        self.seq = Schema(self.NAME, {**root, "$ref": "#/$defs/seq"})


def _sized(data: bytes) -> bytes:
    if len(data) > MAX_MESSAGE:
        raise Refusal("protocol", "a message is too long", fatal=True)
    return data


def _bytes_of(field: str, text: str) -> bytes:
    try:
        return b64url_decode(text)
    except ValueError:
        raise Refusal("protocol", f"{field} is not base64url", fatal=True) from None


class Stage(Enum):
    HELLO = auto()
    SHARE = auto()
    CONFIRM = auto()
    OPEN = auto()  # the exchange is done: messages are encrypted


class Messages:
    """One app's messages, as the robot reads them, and the robot's own to that app, which
    it numbers from 1 (the protocol's Before the exchange). An answer, and an error,
    replies to the message the robot takes, whose ``seq`` is ``answering`` once it can be
    read: an error about a message that cannot be read has no ``reply_to``."""

    def __init__(self, schema: SetupSchema) -> None:
        self._schema = schema
        self._numbers = 0
        self.answering: int | None = None

    def message(self, kind: str, reply_to: int | None = None, **fields: Any) -> dict[str, Any]:
        """A message of the robot's, with its number: ``seq``, from 1 in each session."""
        self._numbers += 1
        message: dict[str, Any] = {"type": kind, "seq": self._numbers}
        if reply_to is not None:
            message["reply_to"] = reply_to
        message.update(fields)
        return message

    def error(self, refusal: Refusal) -> bytes:
        """The error message for ``refusal``, ready to send, replying to the message the
        robot takes if it could be read, its text cut to what the protocol allows."""
        text = refusal.message[:MAX_ERROR]
        return self.pack(self.message("error", self.answering, code=refusal.code, message=text))

    def pack(self, message: Mapping[str, Any]) -> bytes:
        """``message`` as sent: JSON."""
        return encode(message).encode("utf-8")

    def read(self, data: bytes) -> dict[str, Any]:
        """The app's next message, in the clear, checked against the schema. ``Refusal``
        for one that breaks it, and, without anything to reply to, for one that cannot be
        read: not JSON that every side can hold (ai-platform's ADR-0014), not an object,
        or without a ``seq`` (the protocol's Errors). Every such refusal ends the
        session."""
        self.answering = None
        try:
            message = read_json(_sized(data))
        except ValueError as error:
            raise Refusal("protocol", f"a message cannot be read: {error}", fatal=True) from None
        seq = message.get("seq") if isinstance(message, dict) else None
        if self._schema.seq.problem(seq) is not None:
            raise Refusal("protocol", "a message is not an object with a seq", fatal=True)
        self.answering = int(seq)  # an integer, even if it came as 1.0
        problem = self._schema.from_app.problem(message)
        if problem is not None:
            raise Refusal("protocol", f"a message breaks the schema: {problem}", fatal=True)
        return message


class Session(Messages):
    """The robot's side of one session. ``receive`` takes each message the app sends, as
    it came, and returns what to send back; the module answers the requests."""

    def __init__(
        self,
        secret: Secret,
        discriminator: str,
        schema: SetupSchema,
        scalar: int | None = None,  # y; random unless a test replays known values
    ) -> None:
        super().__init__(schema)
        self._secret = secret
        self._discriminator = discriminator
        self._scalar = scalar
        self.stage = Stage.HELLO
        self._app_hello = b""
        self._verifier: Verifier | None = None
        self._records: Records | None = None

    @property
    def attempted(self) -> bool:
        """Whether the app sent its share: an attempt with the code, which fails unless the
        session confirms it."""
        return self.stage in (Stage.CONFIRM, Stage.OPEN)

    def pack(self, message: Mapping[str, Any]) -> bytes:
        """``message`` as sent: JSON, encrypted once the exchange is done."""
        data = super().pack(message)
        return data if self._records is None else self._records.seal(data)

    def receive(self, data: bytes) -> tuple[bytes | None, dict[str, Any] | None]:
        """Take the app's next message. Returns what to send back in the exchange, or,
        once it is done, the request for the module to answer. ``Refusal`` for a message
        out of place: the module answers it, and ends the session if it is fatal; ``Ended``
        when the app ends the session."""
        self.answering = None
        plaintext = _sized(data) if self._records is None else self._records.open(_sized(data))
        message = self.read(plaintext)
        if message["type"] == "error":
            raise Ended(f"the app ended the session: {message['code']}")
        if self._records is not None:
            return None, message
        expected = {Stage.HELLO: "hello", Stage.SHARE: "share", Stage.CONFIRM: "confirm"}
        if message["type"] != expected[self.stage]:
            raise Refusal(
                "protocol", f"expected {expected[self.stage]}, got {message['type']}", fatal=True
            )
        if self.stage is Stage.HELLO:
            return self._hello(data, message), None
        if self.stage is Stage.SHARE:
            return self._share(message), None
        self._confirm(message)
        return None, None

    def _hello(self, data: bytes, message: dict[str, Any]) -> bytes:
        if message["protocol"].split(".")[0] != PROTOCOL.split(".")[0]:
            raise Refusal("version", f"the robot speaks set-up {PROTOCOL}", fatal=True)
        self._app_hello = data
        reply = self.pack(
            self.message(
                "hello",
                message["seq"],
                protocol=PROTOCOL,
                discriminator=self._discriminator,
                salt=b64url(self._secret.salt),
            )
        )
        self._verifier = Verifier(
            _context(self._app_hello, reply),
            self._secret.w0,
            self._secret.record,
            scalar=self._scalar,
        )
        self.stage = Stage.SHARE
        return reply

    def _share(self, message: dict[str, Any]) -> bytes:
        assert self._verifier is not None
        try:
            share, confirmation = self._verifier.respond(_bytes_of("share", message["share"]))
        except ExchangeError as error:
            raise Refusal("protocol", str(error), fatal=True) from None
        self.stage = Stage.CONFIRM
        return self.pack(
            self.message("share", message["seq"], share=b64url(share), confirm=b64url(confirmation))
        )

    def _confirm(self, message: dict[str, Any]) -> None:
        assert self._verifier is not None
        try:
            shared = self._verifier.finish(_bytes_of("confirm", message["confirm"]))
        except ExchangeError:
            raise Refusal("code", "the code is wrong", fatal=True) from None
        app_key, robot_key = _session_keys(shared)
        self._records = Records(robot_key, app_key)
        self.stage = Stage.OPEN


class AppSession:
    """The app's side of a session, for the tests and for tools that play the app: it
    makes the app's messages, and reads the robot's."""

    def __init__(self, code: str, scalar: int | None = None) -> None:
        self._code = code
        self._scalar = scalar  # x; random unless a test replays known values
        self._numbers = 0
        self._hello = b""
        self._prover: Prover | None = None
        self._records: Records | None = None
        self.discriminator: str | None = None

    def _message(self, kind: str, **fields: Any) -> dict[str, Any]:
        self._numbers += 1
        return {"type": kind, "seq": self._numbers, **fields}

    def _pack(self, message: Mapping[str, Any]) -> bytes:
        data = encode(message).encode("utf-8")
        return data if self._records is None else self._records.seal(data)

    def hello(self, protocol: str = PROTOCOL) -> bytes:
        self._hello = self._pack(self._message("hello", protocol=protocol))
        return self._hello

    def share(self, robot_hello: bytes) -> bytes:
        """The app's share, once the robot's hello came."""
        hello = json.loads(robot_hello)
        self.discriminator = hello["discriminator"]
        w0, w1 = stretch(self._code.encode("ascii"), b64url_decode(hello["salt"]))
        self._prover = Prover(_context(self._hello, robot_hello), w0, w1, scalar=self._scalar)
        return self._pack(self._message("share", share=b64url(self._prover.share)))

    def confirm(self, robot_share: bytes) -> bytes:
        """The app's confirmation, once the robot's share came; ``ExchangeError`` if the
        robot's confirmation is wrong."""
        assert self._prover is not None
        reply = json.loads(robot_share)
        confirmation, shared = self._prover.finish(
            b64url_decode(reply["share"]), b64url_decode(reply["confirm"])
        )
        app_key, robot_key = _session_keys(shared)
        message = self._pack(self._message("confirm", confirm=b64url(confirmation)))
        self._records = Records(app_key, robot_key)
        return message

    def request(self, kind: str, **fields: Any) -> bytes:
        """An encrypted request."""
        return self._pack(self._message(kind, **fields))

    def read(self, data: bytes) -> dict[str, Any]:
        """One of the robot's messages: decrypted once the exchange is done."""
        plaintext = data if self._records is None else self._records.open(data)
        message: dict[str, Any] = json.loads(plaintext)
        return message


def ssid_bytes(message: Mapping[str, Any]) -> bytes:
    """The network name a request or a network names, as bytes: ``ssid`` in UTF-8, or
    ``ssid_hex``. ``Refusal`` if it is longer than a network name can be."""
    if "ssid" in message:
        name = message["ssid"].encode("utf-8")
    else:
        name = bytes.fromhex(message["ssid_hex"])
    if not 1 <= len(name) <= SSID_BYTES:
        raise Refusal("protocol", f"a network's name has 1 to {SSID_BYTES} bytes")
    return name


def ssid_fields(name: bytes) -> dict[str, str]:
    """How a message names the network ``name``, as it came from the air: ``ssid`` if it is
    text that every side can hold, UTF-8 without a U+0000 (ai-platform's ADR-0014), else
    ``ssid_hex``."""
    try:
        text = name.decode("utf-8")
    except UnicodeDecodeError:
        return {"ssid_hex": name.hex()}
    return {"ssid_hex": name.hex()} if "\0" in text else {"ssid": text}
