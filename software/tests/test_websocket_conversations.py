# SPDX-License-Identifier: MIT
"""Conversations over the link (platform/protocols/cloud/README.md, Conversations), as
the control channel carries them: asked for, started, refused and ended. Each room is a
fake that the test plays; tests/test_conversation_room.py runs the real one."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
import waiting
from fake_cloud import TIMEOUT, CloudSide, FakeCloud
from test_websocket_cloud import DEVICE, LAMP, SESSION, reconnection, robot_and_cloud

from ai_robot.adapters.websocket.cloud import TERMS, WebSocketCloud
from ai_robot.ports import (
    AudioFormat,
    CloudPart,
    ConversationAudio,
    ConversationEnd,
    ConversationRequest,
    ConversationStart,
    DeviceInfo,
    Interrupted,
    SafetyClass,
    Speech,
    Trigger,
    TurnStart,
)

AUDIO = ConversationAudio(AudioFormat(16000, 1), AudioFormat(16000, 1))
MINE = "01a18086-9160-765a-a90f-9dd0604d45f2"  # a conversation the robot asks for
THEIRS = "01a18086-9160-765a-a90f-9dd0604d45f3"  # one the cloud opens
MEDIA = {"livekit": {"url": "ws://127.0.0.1:7880", "token": "room-token"}}
LISTENING = DeviceInfo(
    "robot",
    "concept-v0",
    ("vi",),
    "Asia/Ho_Chi_Minh",
    {SafetyClass.INFORMATIONAL, SafetyClass.PRIVACY},
)


class FakeRoom:
    """A conversation's room, which the test plays: what the robot hears in it, and how
    it ends."""

    def __init__(self, **settings: Any) -> None:
        self.settings = settings
        self.heard = bytearray(settings["kept"])
        self.interruptions: list[str] = []
        self.left = False
        self._ended: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()

    async def run(self) -> str | None:
        return await self._ended

    async def hear(self, pcm: bytes) -> None:
        self.heard.extend(pcm)

    async def interrupted(self, response: str) -> None:
        self.interruptions.append(response)

    async def leave(self) -> None:
        self.left = True
        if not self._ended.done():
            self._ended.set_result(None)

    def say(self, part: CloudPart) -> None:
        """An agent says ``part``."""
        self.settings["heard"](part)

    def lose(self, reason: str) -> None:
        self._ended.set_result(reason)


def rooms_of(link: WebSocketCloud) -> list[FakeRoom]:
    """The rooms the link makes, from now on."""
    rooms: list[FakeRoom] = []

    def make(**settings: Any) -> FakeRoom:
        rooms.append(FakeRoom(**settings))
        return rooms[-1]

    link.room_factory = make
    return rooms


async def talking(
    link: WebSocketCloud, cloud: FakeCloud, device: DeviceInfo = DEVICE, **hello: Any
) -> tuple[CloudSide, dict[str, Any]]:
    """A session with conversations; returns the cloud's side, and the robot's hello."""
    await link.describe(device)
    await link.offer([LAMP], [], conversation=AUDIO)
    side = await cloud.connection()
    said = await side.next("hello")
    await side.hello(conversations=True, **hello)
    await waiting.until(lambda: link.connection is not None, TIMEOUT)
    return side, said


async def told(link: WebSocketCloud) -> CloudPart:
    """What the link tells the robot next."""
    return await asyncio.wait_for(link.conversation(), TIMEOUT)


async def asked(link: WebSocketCloud, side: CloudSide) -> dict[str, Any]:
    """The robot asks for a conversation; returns the request the cloud received."""
    await link.converse(ConversationRequest(MINE, Trigger.WAKE_WORD, {"battery": 50.0}))
    return await side.next("conversation.request")


def run(main: Callable[[], Any]) -> None:
    asyncio.run(main())


def test_a_robot_that_holds_conversations_says_so() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            _, said = await talking(link, cloud)
            assert (said["conversations"], said["media"]) == (True, ["livekit"])
            assert link.session is not None and link.session.conversations

    run(main)


def test_a_resuming_hello_repeats_the_terms_that_opened_the_session() -> None:
    """A resume keeps the terms that the session's first hello set, and one that changed
    them would get a new session (hello, since 3.7): conversations offered since wait for
    the robot's next session."""

    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            await link.describe(DEVICE)
            await link.offer([LAMP], [])
            side = await cloud.connection()
            first = await side.next("hello")
            await side.hello()
            await waiting.until(lambda: link.connection is not None, TIMEOUT)
            await link.offer([LAMP], [], conversation=AUDIO)
            side, again = await reconnection(cloud, side)
            assert again["resume"] == {"session": SESSION, "received": 0}
            assert {name: again[name] for name in TERMS} == {name: first[name] for name in TERMS}
            assert (again["conversations"], again["media"]) == (False, [])
            await side.hello(resumed=True, received=0)
            await waiting.until(lambda: link.connection is not None, TIMEOUT)
            await side.send({"type": "bye", "reason": "replaced"})  # the session ends
            side = await cloud.connection()
            new = await side.next("hello")
            assert "resume" not in new
            assert (new["conversations"], new["media"]) == (True, ["livekit"])

    run(main)


def test_the_robots_conversation_starts_in_a_room() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            rooms = rooms_of(link)
            side, _ = await talking(link, cloud)
            request = await asked(link, side)
            assert request["conversation"] == MINE and request["trigger"] == "wake_word"
            assert request["context"] == {"battery": 50.0}
            await link.converse(Speech(MINE, b"ab"))  # kept until an agent listens
            start = {"conversation": MINE, "listen": True, "media": MEDIA}
            await side.send({"type": "conversation.start", "reply_to": request["seq"], **start})
            assert await told(link) == ConversationStart(MINE, listen=True)
            await waiting.until(lambda: rooms, TIMEOUT)
            (room,) = rooms
            settings = room.settings
            assert (settings["url"], settings["token"]) == ("ws://127.0.0.1:7880", "room-token")
            assert settings["listen"] is True and settings["conversation"] == MINE
            await link.converse(Speech(MINE, b"cd"))
            assert bytes(room.heard) == b"abcd"
            room.say(TurnStart(MINE, "t1"))
            assert await told(link) == TurnStart(MINE, "t1")
            await link.converse(Interrupted(MINE, "a1"))
            assert room.interruptions == ["a1"]
            await link.converse(ConversationEnd(MINE, "idle"))
            end = await side.next("conversation.end")
            assert (end["conversation"], end["reason"]) == (MINE, "idle")
            assert room.left
            room.say(TurnStart(MINE, "t2"))  # crossed the end: it does not reach the robot
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(link.conversation(), 0.2)
            await link.converse(Speech(MINE, b"ef"))
            assert bytes(room.heard) == b"abcd"

    run(main)


@pytest.mark.parametrize("code", ["limit_reached", "unavailable"])
def test_a_request_the_cloud_refuses_ends_with_its_error(code: str) -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            side, _ = await talking(link, cloud)
            request = await asked(link, side)
            error = {"code": code, "message": "not now", "retry_after": 60}
            await side.send({"type": "error", "reply_to": request["seq"], **error})
            assert await told(link) == ConversationEnd(MINE, code)
            assert side.pending() == []  # the cloud knows: no end to send

    run(main)


def test_a_request_the_cloud_does_not_answer_ends() -> None:
    async def main() -> None:
        async with robot_and_cloud(start_timeout=0.2) as (link, cloud):
            side, _ = await talking(link, cloud)
            await asked(link, side)
            assert await told(link) == ConversationEnd(MINE, "no_answer")
            end = await side.next("conversation.end")
            assert (end["conversation"], end["reason"]) == (MINE, "no_answer")

    run(main)


def test_without_a_session_a_request_ends_at_once() -> None:
    async def main() -> None:
        async with robot_and_cloud(known=False) as (link, _):
            await link.describe(DEVICE)
            await link.offer([], [], conversation=AUDIO)
            await link.converse(ConversationRequest(MINE, Trigger.WAKE_WORD))
            assert await told(link) == ConversationEnd(MINE, "offline")

    run(main)


def test_what_the_robot_heard_before_the_agent_is_bounded() -> None:
    async def main() -> None:
        async with robot_and_cloud(start_timeout=0.05, agent_timeout=0.05) as (link, cloud):
            rooms = rooms_of(link)
            side, _ = await talking(link, cloud)
            request = await asked(link, side)
            heard = bytes(range(256)) * 20  # 5120 bytes; 0.1 s holds 3200
            await link.converse(Speech(MINE, heard))
            start = {"conversation": MINE, "listen": True, "media": MEDIA}
            await side.send({"type": "conversation.start", "reply_to": request["seq"], **start})
            await waiting.until(lambda: rooms, TIMEOUT)
            assert bytes(rooms[0].heard) == heard[-3200:]  # the latest, in whole frames

    run(main)


@pytest.mark.parametrize("listen", [True, False])
def test_the_cloud_opens_a_conversation(listen: bool) -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            rooms = rooms_of(link)
            side, _ = await talking(link, cloud, LISTENING)
            await side.send(
                {
                    "type": "conversation.start",
                    "conversation": THEIRS,
                    "listen": listen,
                    "media": MEDIA,
                }
            )
            assert await told(link) == ConversationStart(THEIRS, listen)
            await waiting.until(lambda: rooms, TIMEOUT)
            assert rooms[0].settings["listen"] is listen
            await side.send({"type": "conversation.end", "conversation": THEIRS, "reason": "done"})
            assert await told(link) == ConversationEnd(THEIRS, "done")
            await waiting.until(lambda: rooms[0].left, TIMEOUT)
            assert side.pending() == []  # nothing to say back

    run(main)


@pytest.mark.parametrize(
    ("device", "microphones", "busy", "code"),
    [
        (DEVICE, True, False, "not_allowed"),  # the owner does not allow the privacy class
        (LISTENING, False, False, "not_allowed"),  # the microphones are off
        (LISTENING, True, True, "busy"),
    ],
)
def test_the_robot_refuses_a_conversation_it_may_not_hold(
    device: DeviceInfo, microphones: bool, busy: bool, code: str
) -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            rooms_of(link)
            side, _ = await talking(link, cloud, device)
            await link.listening(microphones)
            if busy:
                await asked(link, side)
            start = {"conversation": THEIRS, "listen": True, "media": MEDIA}
            sent = await side.send({"type": "conversation.start", **start})
            error = await side.next("error")
            assert (error["reply_to"], error["code"]) == (sent["seq"], code)

    run(main)


def test_a_conversation_whose_room_is_lost_ends() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            rooms = rooms_of(link)
            side, _ = await talking(link, cloud)
            request = await asked(link, side)
            start = {"conversation": MINE, "listen": True, "media": MEDIA}
            await side.send({"type": "conversation.start", "reply_to": request["seq"], **start})
            await told(link)
            await waiting.until(lambda: rooms, TIMEOUT)
            rooms[0].lose("no_agent")
            assert await told(link) == ConversationEnd(MINE, "no_agent")
            end = await side.next("conversation.end")
            assert end["reason"] == "no_agent"

    run(main)


def test_a_room_that_is_not_secure_is_not_joined() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            rooms = rooms_of(link)
            side, _ = await talking(link, cloud)
            request = await asked(link, side)
            media = {"livekit": {"url": "ws://media.example", "token": "t"}}  # SEC-1
            start = {"conversation": MINE, "listen": True, "media": media}
            await side.send({"type": "conversation.start", "reply_to": request["seq"], **start})
            assert await told(link) == ConversationStart(MINE, listen=True)
            assert await told(link) == ConversationEnd(MINE, "error")
            assert (await side.next("conversation.end"))["reason"] == "error"
            assert rooms == []

    run(main)


def test_an_end_the_robot_does_not_know_is_refused_and_a_crossing_one_ignored() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            rooms_of(link)
            side, _ = await talking(link, cloud)
            sent = await side.send(
                {"type": "conversation.end", "conversation": THEIRS, "reason": "done"}
            )
            error = await side.next("error")
            assert (error["reply_to"], error["code"]) == (sent["seq"], "unknown_conversation")
            request = await asked(link, side)
            await link.converse(ConversationEnd(MINE, "idle"))
            await side.next("conversation.end")
            start = {"conversation": MINE, "listen": True, "media": MEDIA}  # crossed the end
            await side.send({"type": "conversation.start", "reply_to": request["seq"], **start})
            await side.send({"type": "conversation.end", "conversation": MINE, "reason": "done"})
            await side.mcp("server/discover")
            assert (await side.next())["type"] == "mcp"  # neither got an answer

    run(main)


def test_a_conversation_outlives_the_control_channel() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            rooms = rooms_of(link)
            side, _ = await talking(link, cloud)
            request = await asked(link, side)
            start = {"conversation": MINE, "listen": True, "media": MEDIA}
            await side.send({"type": "conversation.start", "reply_to": request["seq"], **start})
            await told(link)
            await waiting.until(lambda: rooms, TIMEOUT)
            side, hello = await reconnection(cloud, side)
            assert hello["conversation"] == MINE  # the next hello names it
            assert not rooms[0].left
            await side.hello(SESSION, resumed=True, received=1, sent=1, conversations=True)
            await link.converse(ConversationEnd(MINE, "idle"))
            assert (await side.next("conversation.end"))["reason"] == "idle"

    run(main)
