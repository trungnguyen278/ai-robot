# SPDX-License-Identifier: MIT
"""A conversation's LiveKit room (platform/protocols/cloud/README.md, The room).

The robot joins with the token the cloud gave for the conversation. If it listens, it
publishes its microphone, which only agents may hear, and sends what it heard before
an agent listened as LiveKit's pre-connect audio. It plays only agents' voices, and
takes the conversation's messages only from agents.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Iterator
from typing import Any

from livekit import rtc

from ai_robot.adapters.websocket.session import MAX_MESSAGE, MAX_WARNED, Bucket
from ai_robot.core.data import Schema, encode, read_json
from ai_robot.ports.cloud import (
    CloudPart,
    ConversationAudio,
    ResponseEnd,
    ResponseStart,
    ResponseStop,
    ResponseText,
    SpeechEnd,
    TurnCancel,
    TurnStart,
    Voice,
)

log = logging.getLogger(__name__)

# Fixed by the protocol (platform/protocols/cloud/README.md, The room), and by LiveKit's
# agents, which read the pre-connect audio (livekit-agents, voice/room_io).
TOPIC = "conversation"  # the text streams that carry the conversation's messages
PRE_CONNECT_TOPIC = "lk.agent.pre-connect-audio-buffer"
MICROPHONE = "microphone"  # the name of the robot's track
AGENT = rtc.ParticipantKind.PARTICIPANT_KIND_AGENT

# How this implementation works, which nobody needs to tune.
VOICE_FRAME_MS = 20  # an agent's voice is handed over in pieces this long
PERMISSIONS_AGAIN = 0.5  # seconds after a change of permissions to send them once more


class ConversationRoom:
    """The room of ``conversation``, at ``url``, joined with ``token``.

    With ``listen``, the robot publishes its microphone, and keeps what it hears
    (``hear``) until an agent listens: ``kept``, what it heard before the room, comes
    first. ``heard`` hands the robot each part of the conversation that agents say.
    ``run`` holds the room until the robot leaves it (``leave``), or returns why it was
    lost: ``no_agent`` when no agent came within ``agent_timeout`` seconds, or left and
    none came back; ``media_lost`` when the room could not be joined or kept.
    """

    def __init__(
        self,
        *,
        conversation: str,
        url: str,
        token: str,
        listen: bool,
        audio: ConversationAudio,
        kept: bytes,
        most_kept: int,
        schema: Schema,
        heard: Callable[[CloudPart], None],
        agent_timeout: float,
        connect_timeout: float,
        rate: float,
        burst: int,
        backlog: int,
    ) -> None:
        self.conversation = conversation
        self.url = url
        self.token = token
        self.listen = listen
        self.audio = audio
        self.schema = schema
        self.heard = heard
        self.agent_timeout = agent_timeout
        self.connect_timeout = connect_timeout
        self.most_kept = most_kept
        self.kept = bytearray(kept)  # heard before an agent listens
        self.live = False  # an agent listens: what the robot hears goes to its track
        self.agents: set[str] = set()  # the agents in the room
        self.room = rtc.Room()
        self.source: rtc.AudioSource | None = None
        self._streams: asyncio.Queue[tuple[rtc.TextStreamReader, str]] = asyncio.Queue(
            maxsize=backlog
        )
        self._bucket = Bucket(rate, burst)
        self._voices: dict[str, asyncio.Task[None]] = {}  # track sid -> its reader
        self._tasks: set[asyncio.Task[None]] = set()
        self._again: asyncio.TimerHandle | None = None  # sends the permissions once more
        self._waiting: asyncio.TimerHandle | None = None  # agent_timeout
        self._ended: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
        self._left = False
        self._warned: set[str] = set()
        self.room.register_text_stream_handler(TOPIC, self._on_stream)
        self.room.on("participant_connected", self._on_joined)
        self.room.on("participant_disconnected", self._on_left)
        self.room.on("track_published", self._on_published)
        self.room.on("track_subscribed", self._on_subscribed)
        self.room.on("track_unsubscribed", self._on_unsubscribed)
        self.room.on("local_track_subscribed", self._on_listened)
        self.room.on("disconnected", self._on_disconnected)

    # ---- its life ----

    async def run(self) -> str | None:
        """Join, then hold the room. Returns ``None`` once the robot leaves it, or why
        the room was lost."""
        if self._left:
            return None  # the conversation ended before its room was joined
        options = rtc.RoomOptions(auto_subscribe=False, connect_timeout=self.connect_timeout)
        try:
            await self.room.connect(self.url, self.token, options)
            if self._left:  # it ended while the robot joined
                with contextlib.suppress(Exception):
                    await self.room.disconnect()
                return None
            if self.listen:
                await self._publish()
        except Exception as error:  # the room is out of reach, or refuses the token
            if self._left:
                return None
            log.warning("cannot join the conversation's room: %s", error or type(error).__name__)
            return "media_lost"
        for participant in list(self.room.remote_participants.values()):
            self._on_joined(participant)
        self._wait_for_an_agent()
        reader = asyncio.create_task(self._read_streams(), name="conversation: messages")
        try:
            return await self._ended
        finally:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader

    async def leave(self) -> None:
        """Leave the room: the conversation is over."""
        if self._left:
            return
        self._left = True
        self._end(None)
        for timer in (self._again, self._waiting):
            if timer is not None:
                timer.cancel()
        for task in [*self._voices.values(), *self._tasks]:
            task.cancel()
        if self.source is not None:
            with contextlib.suppress(Exception):
                await self.source.aclose()
        with contextlib.suppress(Exception):
            await self.room.disconnect()

    def _end(self, reason: str | None) -> None:
        if not self._ended.done():
            self._ended.set_result(reason)

    # ---- what the robot hears ----

    async def _publish(self) -> None:
        speech = self.audio.speech
        self.source = rtc.AudioSource(speech.sample_rate, speech.channels)
        track = rtc.LocalAudioTrack.create_audio_track(MICROPHONE, self.source)
        self._allow()  # before the track exists, so that nobody may ever take it
        options = rtc.TrackPublishOptions(
            source=rtc.TrackSource.SOURCE_MICROPHONE, preconnect_buffer=True
        )
        await self.room.local_participant.publish_track(track, options)

    async def hear(self, pcm: bytes) -> None:
        """What the robot hears: kept until an agent listens, then streamed live."""
        if not self.live:
            self.kept.extend(pcm)
            excess = len(self.kept) - self.most_kept
            if excess > 0:
                frame = self.audio.speech.frame_bytes
                del self.kept[: -(-excess // frame) * frame]
            return
        source = self.source
        speech = self.audio.speech
        samples = len(pcm) // speech.frame_bytes
        if source is None or not samples or self._left:
            return
        data = pcm[: samples * speech.frame_bytes]
        frame = rtc.AudioFrame(data, speech.sample_rate, speech.channels, samples)
        with self._guard("send what the robot hears"):
            await source.capture_frame(frame)

    def _on_listened(self, track: rtc.LocalTrack) -> None:
        """An agent subscribed to the microphone: it hears what was kept, then the
        track."""
        if self.live:
            return
        self.live = True
        kept, self.kept = bytes(self.kept), bytearray()
        if kept and self.agents:
            self._spawn(self._send_kept(kept, track.sid), "conversation: pre-connect audio")

    async def _send_kept(self, pcm: bytes, track: str) -> None:
        speech = self.audio.speech
        with self._guard("send what it heard before the agent"):
            writer = await self.room.local_participant.stream_bytes(
                "preconnect-buffer",
                topic=PRE_CONNECT_TOPIC,
                destination_identities=sorted(self.agents),
                attributes={
                    "trackId": track,
                    "sampleRate": str(speech.sample_rate),
                    "channels": str(speech.channels),
                },
            )
            await writer.write(pcm)
            await writer.aclose()

    def _allow(self) -> None:
        """Let only the agents subscribe to the robot's microphone (PRIV-1).

        The SDK sends each change from a task of its own (livekit-ffi's
        ``set_track_subscription_permissions``), so two changes close together may reach
        the server in either order. The latest is sent once more after them, to be the
        last word.
        """
        if not self.listen:
            return
        self._send_permissions()
        if self._again is not None:
            self._again.cancel()
        loop = asyncio.get_running_loop()
        self._again = loop.call_later(PERMISSIONS_AGAIN, self._send_permissions)

    def _send_permissions(self) -> None:
        permissions = [
            rtc.ParticipantTrackPermission(participant_identity=agent, allow_all=True)
            for agent in sorted(self.agents)
        ]
        with self._guard("set who may hear the robot"):
            self.room.local_participant.set_track_subscription_permissions(
                allow_all_participants=False, participant_permissions=permissions
            )

    # ---- agents ----

    def _on_joined(self, participant: rtc.RemoteParticipant) -> None:
        if participant.kind != AGENT:
            identity = participant.identity
            self._once(identity, "ignoring %s in the room: not an agent", identity)
            return
        self.agents.add(participant.identity)
        self._allow()
        for publication in participant.track_publications.values():
            if publication.kind == rtc.TrackKind.KIND_AUDIO:
                publication.set_subscribed(True)

    def _on_left(self, participant: rtc.RemoteParticipant) -> None:
        if participant.identity not in self.agents:
            return
        log.info("the agent %s left the conversation's room", participant.identity)
        self.agents.discard(participant.identity)
        self._allow()
        if not self.agents:
            self._wait_for_an_agent()

    def _wait_for_an_agent(self) -> None:
        """Give an agent ``agent_timeout`` seconds to come, and to listen."""
        if self._waiting is not None:
            self._waiting.cancel()
        loop = asyncio.get_running_loop()
        self._waiting = loop.call_later(self.agent_timeout, self._check_for_an_agent)

    def _check_for_an_agent(self) -> None:
        if self.agents and (self.live or not self.listen):
            return
        log.warning("no agent in the conversation's room within %g s", self.agent_timeout)
        self._end("no_agent")

    def _on_disconnected(self, reason: Any) -> None:
        if not self._left:
            log.warning("lost the conversation's room: %s", reason)
            self._end("media_lost")

    # ---- what agents say ----

    def _on_published(
        self, publication: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant
    ) -> None:
        if participant.identity in self.agents and publication.kind == rtc.TrackKind.KIND_AUDIO:
            publication.set_subscribed(True)

    def _on_subscribed(
        self,
        track: rtc.Track,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        if participant.identity in self.agents and track.kind == rtc.TrackKind.KIND_AUDIO:
            self._voices[track.sid] = asyncio.create_task(
                self._voice(track), name="conversation: an agent's voice"
            )

    def _on_unsubscribed(
        self,
        track: rtc.Track,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        task = self._voices.pop(track.sid, None)
        if task is not None:
            task.cancel()

    async def _voice(self, track: rtc.Track) -> None:
        """Hand an agent's voice to the robot as it plays, in the robot's format."""
        voice = self.audio.voice
        stream = rtc.AudioStream.from_track(
            track=track,
            sample_rate=voice.sample_rate,
            num_channels=voice.channels,
            frame_size_ms=VOICE_FRAME_MS,
        )
        try:
            async for event in stream:
                self.heard(Voice(self.conversation, bytes(event.frame.data)))
        except Exception as error:  # the track went away with its agent or the room
            log.warning("an agent's voice stopped: %s", error or type(error).__name__)
        finally:
            await stream.aclose()

    def _on_stream(self, reader: rtc.TextStreamReader, identity: str) -> None:
        try:
            self._streams.put_nowait((reader, identity))
        except asyncio.QueueFull:
            self._once("backlog", "dropping the agents' messages: too many at once")

    async def _read_streams(self) -> None:
        """Take the agents' messages in the order they came, at the robot's rate."""
        while True:
            reader, identity = await self._streams.get()
            try:
                text = await _read(reader)
            except Exception as error:  # a stream cut off on its way; the next may be whole
                log.warning("a message from %s was cut off: %s", identity, error)
                continue
            if identity not in self.agents:
                self._once(identity, "ignoring messages from %s: not an agent", identity)
                continue
            await self._bucket.take()
            if text is None:
                self._once("size", "ignoring messages from the agent that are too large")
                continue
            part = self._part(text)
            if part is not None:
                self.heard(part)

    def _part(self, text: str) -> CloudPart | None:
        """The part of the conversation that a message from an agent says, if any. A
        message that is not valid is ignored: the room has no errors."""
        try:
            message = read_json(text)
        except ValueError:
            self._once(
                "json", "ignoring messages from the agent that are not JSON every side holds"
            )
            return None
        problem = self.schema.problem(message)
        if problem is not None:
            log.warning("ignoring a message from the agent: %s", problem)
            return None
        conversation, kind = self.conversation, message["type"]
        if kind == "turn.start":
            return TurnStart(conversation, message["turn"])
        if kind == "turn.speech_end":
            return SpeechEnd(conversation, message["turn"])
        if kind == "turn.cancel":
            return TurnCancel(conversation, message["turn"], message["reason"])
        if kind == "response.start":
            response, turn, language = message["response"], message["turn"], message["language"]
            return ResponseStart(conversation, response, turn, language)
        if kind == "response.text":
            return ResponseText(conversation, message["response"], message["delta"])
        if kind == "response.end":
            return ResponseEnd(conversation, message["response"])
        if kind == "response.stop":
            return ResponseStop(conversation, message["response"])
        self._once(kind, "ignoring %s messages from the agent", kind)
        return None

    async def interrupted(self, response: str) -> None:
        """The user spoke over ``response``: the robot stopped playing it."""
        if not self.agents or self._left:
            return
        message = encode({"type": "response.interrupted", "response": response})
        with self._guard("tell the agent of an interruption"):
            await self.room.local_participant.send_text(
                message, topic=TOPIC, destination_identities=sorted(self.agents)
            )

    # ---- helpers ----

    def _spawn(self, work: Any, name: str) -> None:
        task: asyncio.Task[None] = asyncio.create_task(work, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _once(self, key: str, text: str, *args: object) -> None:
        if key not in self._warned and len(self._warned) < MAX_WARNED:
            self._warned.add(key)
            log.info(text, *args)

    @contextlib.contextmanager
    def _guard(self, what: str) -> Iterator[None]:
        try:
            yield
        except Exception as error:  # the room is going away; the conversation must survive
            log.warning("cannot %s: %s", what, error or type(error).__name__)


async def _read(reader: rtc.TextStreamReader) -> str | None:
    """The whole message, or ``None`` if it is larger than the protocol allows."""
    chunks: list[str] = []
    size = 0
    async for chunk in reader:
        size += len(chunk.encode())
        if size > MAX_MESSAGE:
            return None
        chunks.append(chunk)
    return "".join(chunks)
