# SPDX-License-Identifier: MIT
"""A conversation's room, through a real LiveKit server, with a test playing the cloud's
agent (platform/protocols/cloud/README.md, The room).

The tests need the LiveKit server: the path to `livekit-server` in the environment
variable LIVEKIT_SERVER, or the program on the PATH. Without it they are skipped. The
software workflow downloads it, so they always run in CI.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import shutil
import struct
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import waiting
from fake_cloud import schema
from livekit import api, rtc
from livekit.rtc._proto.track_pb2 import AudioTrackFeature  # as LiveKit's agents read it
from local_servers import free_port, free_udp_port, running
from test_websocket_cloud import robot_and_cloud
from test_websocket_conversations import talking, told

from ai_robot.adapters.websocket.room import PRE_CONNECT_TOPIC, TOPIC, ConversationRoom
from ai_robot.adapters.websocket.session import MAX_WARNED
from ai_robot.ports import (
    AudioFormat,
    CloudPart,
    ConversationAudio,
    ConversationEnd,
    ConversationRequest,
    ConversationStart,
    ResponseStart,
    Speech,
    Trigger,
    TurnStart,
    Voice,
)

KEY = "test"
SECRET = "a-test-secret-that-is-long-enough-for-livekit"
TIMEOUT = 10.0
AUDIO = ConversationAudio(AudioFormat(16000, 1), AudioFormat(16000, 1))
CONVERSATION = "01a18086-9160-765a-a90f-9dd0604d45f2"
TURN = "01a18086-9854-7225-8534-4e2bfd22bb42"
RESPONSE = "01a18086-9e30-7000-8000-000000000001"


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A LiveKit server on this computer, for these tests only; its address."""
    program = os.environ.get("LIVEKIT_SERVER") or shutil.which("livekit-server")
    if not program:
        pytest.skip("no LiveKit server: set LIVEKIT_SERVER, or put livekit-server on the PATH")
    port = free_port()
    config = (
        f"port: {port}\n"
        "rtc:\n"
        f"  udp_port: {free_udp_port()}\n"
        f"  tcp_port: {free_port()}\n"
        "  use_external_ip: false\n"
    )
    command = [
        program,
        "--bind",
        "127.0.0.1",
        "--keys",
        f"{KEY}: {SECRET}",
        "--config-body",
        config,
    ]
    with running(command, port, tmp_path_factory.mktemp("livekit") / "server.log", TIMEOUT):
        yield f"ws://127.0.0.1:{port}"


def token(room: str, identity: str, kind: str = "standard") -> str:
    """A token for ``room``, as the cloud makes one for each who joins it."""
    grants = api.VideoGrants(
        room_join=True, room=room, can_publish=True, can_subscribe=True, can_publish_data=True
    )
    return (
        api.AccessToken(KEY, SECRET)
        .with_identity(identity)
        .with_kind(kind)  # type: ignore[arg-type]
        .with_grants(grants)
        .to_jwt()
    )


def tone(seconds: float, rate: int = 16000, amplitude: int = 8000) -> bytes:
    """A 440 Hz tone, as 16-bit PCM, mono."""
    count = round(seconds * rate)
    return struct.pack(
        f"<{count}h",
        *(round(amplitude * math.sin(2 * math.pi * 440 * n / rate)) for n in range(count)),
    )


def loud(pcm: bytes) -> bool:
    samples = memoryview(pcm).cast("h")
    return max((abs(sample) for sample in samples), default=0) > 1000


class Agent:
    """Someone else in the conversation's room: the cloud's agent, or anyone who is not.
    It takes what the robot publishes, as LiveKit's agents do."""

    def __init__(self) -> None:
        self.room = rtc.Room()
        self.kept: asyncio.Future[tuple[bytes, dict[str, str]]] = (
            asyncio.get_running_loop().create_future()
        )
        self.frames: list[bytes] = []  # what it hears on the robot's microphone
        self.messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.features: list[int] = []  # those of the robot's microphone
        self._tasks: list[asyncio.Task[None]] = []
        self.room.register_byte_stream_handler(PRE_CONNECT_TOPIC, self._on_kept)
        self.room.register_text_stream_handler(TOPIC, self._on_message)
        self.room.on("track_subscribed", self._on_track)

    async def join(self, url: str, room: str, identity: str, kind: str) -> None:
        await self.room.connect(url, token(room, identity, kind))

    async def leave(self) -> None:
        for task in self._tasks:
            task.cancel()
        await self.room.disconnect()

    def _on_kept(self, reader: rtc.ByteStreamReader, identity: str) -> None:
        async def read() -> None:
            data = b"".join([chunk async for chunk in reader])
            self.kept.set_result((data, dict(reader.info.attributes)))

        self._tasks.append(asyncio.ensure_future(read()))

    def _on_message(self, reader: rtc.TextStreamReader, identity: str) -> None:
        async def read() -> None:
            self.messages.put_nowait(json.loads(await reader.read_all()))

        self._tasks.append(asyncio.ensure_future(read()))

    def _on_track(self, track: rtc.Track, publication: rtc.RemoteTrackPublication, *_: Any) -> None:
        self.features = list(publication.audio_features)

        async def listen() -> None:
            stream = rtc.AudioStream.from_track(track=track, sample_rate=16000, num_channels=1)
            async for event in stream:
                self.frames.append(bytes(event.frame.data))

        self._tasks.append(asyncio.ensure_future(listen()))

    async def say(self, message: dict[str, Any]) -> None:
        """Say ``message`` to the robot, in the room."""
        await self.room.local_participant.send_text(
            json.dumps(message), topic=TOPIC, destination_identities=["robot"]
        )

    async def speak(self, pcm: bytes) -> None:
        """Speak ``pcm`` on a track of its own."""
        source = rtc.AudioSource(16000, 1)
        track = rtc.LocalAudioTrack.create_audio_track("voice", source)
        options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        await self.room.local_participant.publish_track(track, options)
        for start in range(0, len(pcm), 640):
            piece = pcm[start : start + 640]
            await source.capture_frame(rtc.AudioFrame(piece, 16000, 1, len(piece) // 2))


@contextlib.asynccontextmanager
async def robot_room(
    url: str, *, listen: bool = True, kept: bytes = b"", agent_timeout: float = TIMEOUT
) -> AsyncIterator[tuple[ConversationRoom, asyncio.Queue[CloudPart], str, asyncio.Task[Any]]]:
    """The robot in a conversation's room of its own: the room, what agents say in it,
    its name, and the task that holds it."""
    name = f"conversation-{uuid.uuid4().hex[:8]}"
    parts: asyncio.Queue[CloudPart] = asyncio.Queue()
    room = ConversationRoom(
        conversation=CONVERSATION,
        url=url,
        token=token(name, "robot"),
        listen=listen,
        audio=AUDIO,
        kept=kept,
        most_kept=10**7,
        schema=schema("from_agent"),
        heard=parts.put_nowait,
        agent_timeout=agent_timeout,
        connect_timeout=TIMEOUT,
        rate=1000.0,
        burst=1000,
        backlog=500,
    )
    holding = asyncio.create_task(room.run())
    try:
        published = lambda: bool(room.room.local_participant.track_publications)  # noqa: E731
        await waiting.until(
            lambda: holding.done() or (room.room.isconnected() and (not listen or published())),
            TIMEOUT,
        )
        yield room, parts, name, holding
    finally:
        await room.leave()
        await asyncio.wait_for(holding, TIMEOUT)


async def next_part(parts: asyncio.Queue[CloudPart]) -> CloudPart:
    return await asyncio.wait_for(parts.get(), TIMEOUT)


def test_only_agents_hear_the_robot_and_they_hear_what_came_before(server: str) -> None:
    async def main() -> None:
        kept = tone(0.4)
        async with robot_room(server, kept=kept) as (room, _, name, _):
            phone, agent = Agent(), Agent()
            await phone.join(server, name, "phone", "standard")
            await agent.join(server, name, "agent", "agent")
            try:
                heard, attributes = await asyncio.wait_for(agent.kept, TIMEOUT)
                assert heard == kept  # LiveKit's pre-connect audio: 16-bit PCM
                (microphone,) = room.room.local_participant.track_publications
                assert attributes == {"trackId": microphone, "sampleRate": "16000", "channels": "1"}
                # The agent's own event for the track may come after the audio it kept.
                await waiting.until(lambda: agent.features, TIMEOUT)
                assert AudioTrackFeature.TF_PRECONNECT_BUFFER in agent.features
                for _ in range(50):
                    await room.hear(tone(0.02))
                await waiting.until(lambda: any(loud(frame) for frame in agent.frames), TIMEOUT)
                assert phone.frames == [] and not phone.kept.done()  # PRIV-1
            finally:
                await phone.leave()
                await agent.leave()

    asyncio.run(main())


def test_the_agents_say_the_conversation_and_no_one_else_does(server: str) -> None:
    async def main() -> None:
        async with robot_room(server, listen=False) as (room, parts, name, _):
            phone, agent = Agent(), Agent()
            await phone.join(server, name, "phone", "standard")
            await agent.join(server, name, "agent", "agent")
            try:
                await waiting.until(lambda: "agent" in room.agents, TIMEOUT)
                await phone.say({"type": "turn.start", "turn": TURN})  # not an agent
                await agent.say({"type": "turn.start", "turn": "not a turn"})  # not valid
                await agent.say({"type": "mood.changed", "mood": "calm"})  # not known
                await agent.say({"type": "turn.start", "turn": TURN})
                start = {"response": RESPONSE, "turn": TURN, "language": "vi"}
                await agent.say({"type": "response.start", **start})
                assert await next_part(parts) == TurnStart(CONVERSATION, TURN)
                assert await next_part(parts) == ResponseStart(CONVERSATION, RESPONSE, TURN, "vi")
                await agent.speak(tone(1.0))
                while True:
                    part = await next_part(parts)
                    assert isinstance(part, Voice) and part.conversation == CONVERSATION
                    if loud(part.pcm):
                        break
            finally:
                await phone.leave()
                await agent.leave()

    asyncio.run(main())


def test_an_interruption_reaches_the_agents(server: str) -> None:
    async def main() -> None:
        async with robot_room(server, listen=False) as (room, _, name, _):
            agent = Agent()
            await agent.join(server, name, "agent", "agent")
            try:
                await waiting.until(lambda: "agent" in room.agents, TIMEOUT)
                await room.interrupted(RESPONSE)
                message = await asyncio.wait_for(agent.messages.get(), TIMEOUT)
                assert message == {"type": "response.interrupted", "response": RESPONSE}
                assert schema("from_device_in_room").problem(message) is None
            finally:
                await agent.leave()

    asyncio.run(main())


def test_a_room_no_agent_listens_in_is_given_up(server: str) -> None:
    async def main() -> None:
        async with robot_room(server, agent_timeout=0.5) as (_, _, name, holding):
            phone = Agent()
            await phone.join(server, name, "phone", "standard")  # not an agent
            try:
                assert await asyncio.wait_for(holding, TIMEOUT) == "no_agent"
            finally:
                await phone.leave()

    asyncio.run(main())


def test_a_room_that_refuses_the_robot_is_lost(server: str) -> None:
    async def main() -> None:
        room = ConversationRoom(
            conversation=CONVERSATION,
            url=server,
            token="not a token",
            listen=True,
            audio=AUDIO,
            kept=b"",
            most_kept=10**7,
            schema=schema("from_agent"),
            heard=lambda part: None,
            agent_timeout=TIMEOUT,
            connect_timeout=TIMEOUT,
            rate=1000.0,
            burst=1000,
            backlog=500,
        )
        try:
            assert await asyncio.wait_for(room.run(), TIMEOUT) == "media_lost"
        finally:
            await room.leave()

    asyncio.run(main())


def test_a_conversation_from_the_wake_word_to_the_agent(server: str) -> None:
    """The whole way: the robot asks the cloud, joins the room the cloud names, and the
    agent hears what the robot heard from its wake word on, then answers."""

    async def main() -> None:
        name = f"conversation-{uuid.uuid4().hex[:8]}"
        async with robot_and_cloud(agent_timeout=TIMEOUT) as (link, cloud):
            side, _ = await talking(link, cloud)
            await link.converse(ConversationRequest(CONVERSATION, Trigger.WAKE_WORD))
            request = await side.next("conversation.request")
            before = tone(0.3)
            await link.converse(Speech(CONVERSATION, before))  # before the cloud answers
            media = {"livekit": {"url": server, "token": token(name, "robot")}}
            start = {"conversation": CONVERSATION, "listen": True, "media": media}
            await side.send({"type": "conversation.start", "reply_to": request["seq"], **start})
            assert await told(link) == ConversationStart(CONVERSATION, listen=True)
            agent = Agent()
            await agent.join(server, name, "agent", "agent")
            try:
                heard, _ = await asyncio.wait_for(agent.kept, TIMEOUT)
                assert heard == before
                await agent.say({"type": "turn.start", "turn": TURN})
                assert await told(link) == TurnStart(CONVERSATION, TURN)
            finally:
                await agent.leave()
            await link.converse(ConversationEnd(CONVERSATION, "idle"))
            assert (await side.next("conversation.end"))["reason"] == "idle"

    asyncio.run(main())


def test_a_room_left_before_it_is_joined_is_never_joined(server: str) -> None:
    async def main() -> None:
        name = f"conversation-{uuid.uuid4().hex[:8]}"
        room = ConversationRoom(
            conversation=CONVERSATION,
            url=server,
            token=token(name, "robot"),
            listen=True,
            audio=AUDIO,
            kept=b"",
            most_kept=10**7,
            schema=schema("from_agent"),
            heard=lambda part: None,
            agent_timeout=TIMEOUT,
            connect_timeout=TIMEOUT,
            rate=1000.0,
            burst=1000,
            backlog=500,
        )
        await room.leave()  # the conversation ended at once
        assert await asyncio.wait_for(room.run(), TIMEOUT) is None
        assert not room.room.isconnected()

    asyncio.run(main())


def test_the_kinds_of_message_ignored_are_logged_once_and_at_most_max_warned(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def main() -> None:
        room = ConversationRoom(
            conversation=CONVERSATION,
            url="ws://127.0.0.1:9",  # never joined
            token="not a token",
            listen=True,
            audio=AUDIO,
            kept=b"",
            most_kept=10**7,
            schema=schema("from_agent"),
            heard=lambda part: None,
            agent_timeout=TIMEOUT,
            connect_timeout=TIMEOUT,
            rate=1000.0,
            burst=1000,
            backlog=500,
        )
        for kind in [f"other.k{number}" for number in range(MAX_WARNED + 1)] * 2:
            assert room._part(json.dumps({"type": kind})) is None

    with caplog.at_level(logging.INFO, logger="ai_robot.adapters.websocket.room"):
        asyncio.run(main())
    ignored = [line for line in caplog.messages if line.startswith("ignoring other.k")]
    assert ignored == [
        f"ignoring other.k{number} messages from the agent" for number in range(MAX_WARNED)
    ]
