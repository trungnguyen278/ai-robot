# SPDX-License-Identifier: MIT
"""systemd's service manager, as the factory reset's tests need it (org.freedesktop.systemd1(5),
read 2026-11-19): on a private D-Bus, a manager that starts a unit as a job and says when
the job ended, to the clients that subscribed only. And its factory reset service, on
Varlink."""

from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path
from typing import Any

from dbus_fast import Message, MessageType
from dbus_fast.aio import MessageBus

SYSTEMD = "org.freedesktop.systemd1"
SYSTEMD_PATH = "/org/freedesktop/systemd1"
MANAGER = f"{SYSTEMD}.Manager"


class FakeSystemd:
    """Owns ``org.freedesktop.systemd1`` on the bus at ``address``. Each ``StartUnit`` makes
    a job, which ends at once with ``result``; ``early`` says so before the call's answer,
    and ``hold`` waits for ``finish``. ``calls`` lists the methods called, with their
    arguments; ``caller`` is the connection that last started a unit, and ``started`` is
    set once one did."""

    def __init__(self, address: str) -> None:
        self.address = address
        self.result = "done"
        self.early = False
        self.hold = False
        self.calls: list[tuple[str, list[Any]]] = []
        self.caller: str | None = None
        self.started = asyncio.Event()
        self.bus: MessageBus | None = None
        self._subscribed = False
        self._numbers = itertools.count(1)
        self._held: tuple[int, str, str] | None = None
        self.service: FakeFactoryResetService | None = None  # the tests set it

    async def start(self) -> None:
        self.bus = await MessageBus(bus_address=self.address).connect()
        await self.bus.request_name(SYSTEMD)
        self.bus.add_message_handler(self._handle)

    async def stop(self) -> None:
        if self.bus is not None:
            self.bus.disconnect()

    def finish(self, result: str) -> None:
        """End the job held, with ``result``."""
        assert self._held is not None
        self._removed(*self._held, result)
        self._held = None

    def another_job_ends(self, unit: str, result: str) -> None:
        """Another job ends, as systemd tells every subscriber of every job."""
        number = next(self._numbers)
        self._removed(number, f"{SYSTEMD_PATH}/job/{number}", unit, result)

    def _handle(self, message: Message) -> bool | None:
        if message.message_type is not MessageType.METHOD_CALL or message.interface != MANAGER:
            return None
        self.calls.append((message.member, list(message.body)))
        if message.member == "Subscribe":
            self._subscribed = True  # systemd sends its signals once a client subscribed
            self._send(Message.new_method_return(message))
            return True
        if message.member == "StartUnit":
            unit, _mode = message.body
            number = next(self._numbers)
            job = (number, f"{SYSTEMD_PATH}/job/{number}", unit)
            answer = Message.new_method_return(message, "o", [job[1]])
            self.caller = message.sender
            if self.hold:
                self._held = job
                self._send(answer)
            elif self.early:
                self._removed(*job, self.result)
                self._send(answer)
            else:
                self._send(answer)
                self._removed(*job, self.result)
            self.started.set()
            return True
        return None

    def _removed(self, number: int, job: str, unit: str, result: str) -> None:
        if self._subscribed:
            self._send(
                Message.new_signal(
                    SYSTEMD_PATH, MANAGER, "JobRemoved", "uoss", [number, job, unit, result]
                )
            )

    def _send(self, message: Message) -> None:
        assert self.bus is not None
        self.bus.send(message)


class FakeFactoryResetService:
    """systemd's factory reset service on Varlink (systemd-factory-reset(8), read
    2026-11-19), at the Unix socket ``path``: it reports ``mode``. ``reply``, if set, is
    sent instead of its answer, as it is; empty, it hangs up. ``calls`` lists the methods
    called."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.mode = "unspecified"  # what Ubuntu's systemd 259 reports on a Pi 5
        self.reply: bytes | None = None
        self.calls: list[str] = []
        self.server: asyncio.Server | None = None

    async def start(self) -> None:
        self.server = await asyncio.start_unix_server(self._serve, path=str(self.path))

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                call = json.loads((await reader.readuntil(b"\0"))[:-1])
                self.calls.append(call["method"])
                if self.reply == b"":
                    return
                if self.reply is not None:
                    writer.write(self.reply)
                elif call["method"] == "io.systemd.FactoryReset.GetFactoryResetMode":
                    writer.write(json.dumps({"parameters": {"mode": self.mode}}).encode() + b"\0")
                else:
                    error = {"error": "org.varlink.service.MethodNotFound", "parameters": {}}
                    writer.write(json.dumps(error).encode() + b"\0")
                await writer.drain()
        except asyncio.IncompleteReadError:
            pass  # the client is gone
        finally:
            writer.close()
