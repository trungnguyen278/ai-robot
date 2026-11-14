# SPDX-License-Identifier: MIT
"""The voice module: conversations, their turns, answers and limits, and barge-in
(docs/decisions/0013 and 0018), with microphones, a speaker and engines the tests
control."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from ai_robot.adapters.microwakeword.wake_word import MicroWakeWords
from ai_robot.adapters.silero.voice_activity import SileroVoiceActivity
from ai_robot.core.bus import Bus
from ai_robot.core.module import ModuleContext, RobotInfo
from ai_robot.messages import (
    CloudStatus,
    ConversationFromCloud,
    ConversationOffered,
    ConversationToCloud,
    ExpressionRequest,
    InteractionChanged,
    InteractionState,
    MicrophonesChanged,
    SettingChanged,
    SettingOffered,
    SoundLevel,
    WakeReason,
    WakeUp,
)
from ai_robot.modules.voice import VoiceModule
from ai_robot.ports import (
    AudioFormat,
    AudioIn,
    AudioOut,
    CloudPart,
    ConversationAudio,
    ConversationEnd,
    ConversationRequest,
    ConversationStart,
    Interrupted,
    ResponseEnd,
    ResponseStart,
    ResponseStop,
    RobotPart,
    Speech,
    SpeechEnd,
    Trigger,
    TurnCancel,
    TurnStart,
    Voice,
    VoiceActivity,
    WakeWord,
)

FORMAT = AudioFormat(16000, 1)
CHUNK = 640  # 20 ms
SILENCE = bytes(CHUNK)
VOICE = b"V" + bytes(CHUNK - 1)  # what the fake detector takes for voice
WAKE = b"W" + bytes(CHUNK - 1)  # what the fake spotter takes for the wake word
SETTINGS = {
    "voice_threshold": 0.5,
    "nothing_heard": 2.0,
    "barge_in": 0.2,
    "longest": 30.0,
    "offline_expression": "confused",
    "offline_seconds": 2.0,
    "volume": 100,
    "microphones": True,
    "idle": 5,
    "idle_least": 5,
    "idle_most": 30,
}
TIMEOUT = 2.0
ENGINES_TIMEOUT = 30.0  # for the real engines on a whole recording, however slow the computer
REMINDER = "01a18086-9160-765a-a90f-9dd0604d45f2"  # a conversation the cloud opens


def chunks(seconds: float, chunk: bytes) -> list[bytes]:
    return [chunk] * round(seconds / 0.02)


class Microphones(AudioIn):
    """Hear what the test gives them; ``heard`` waits until the module has handled it
    all and asks for more."""

    def __init__(self, echo_cancelled: bool) -> None:
        self.chunks: asyncio.Queue[bytes] = asyncio.Queue()
        self.waiting = asyncio.Event()
        self._echo_cancelled = echo_cancelled

    @property
    def format(self) -> AudioFormat:
        return FORMAT

    @property
    def echo_cancelled(self) -> bool:
        return self._echo_cancelled

    async def read(self) -> bytes:
        if self.chunks.empty():
            self.waiting.set()
        chunk = await self.chunks.get()
        self.waiting.clear()
        return chunk

    async def heard(self, *chunks: bytes, timeout: float = TIMEOUT) -> None:
        self.waiting.clear()
        for chunk in chunks:
            self.chunks.put_nowait(chunk)
        await asyncio.wait_for(self.waiting.wait(), timeout)


class Speaker(AudioOut):
    def __init__(self) -> None:
        self.played = bytearray()
        self.drops = 0
        self.playing = asyncio.Event()  # cleared to hold drain() back
        self.playing.set()

    @property
    def format(self) -> AudioFormat:
        return FORMAT

    async def write(self, pcm: bytes) -> None:
        self.played.extend(pcm)

    async def drain(self) -> None:
        await self.playing.wait()

    async def drop(self) -> None:
        self.drops += 1


class Spotter(WakeWord):
    def __init__(self, format: AudioFormat = FORMAT) -> None:
        self._format = format
        self.resets = 0

    @property
    def format(self) -> AudioFormat:
        return self._format

    async def spot(self, pcm: bytes) -> str | None:
        return "hey robot" if pcm[:1] == b"W" else None

    def reset(self) -> None:
        self.resets += 1


class Detector(VoiceActivity):
    @property
    def format(self) -> AudioFormat:
        return FORMAT

    async def voice(self, pcm: bytes) -> float:
        return 1.0 if pcm[:1] == b"V" else 0.0

    def reset(self) -> None:
        pass


class Robot:
    """The voice module, its devices, and what it says on the bus."""

    def __init__(self, echo_cancelled: bool = True, **changes: Any) -> None:
        self.bus = Bus()
        self.microphones = Microphones(echo_cancelled)
        self.speaker = Speaker()
        self.spotter = Spotter()
        ports = {
            "audio_in": self.microphones,
            "audio_out": self.speaker,
            "wake_word": self.spotter,
            "voice_activity": Detector(),
        }
        ctx = ModuleContext(
            name="voice",
            bus=self.bus,
            ports=ports,
            robot=RobotInfo(display_name="Robot", kind="robot", model="concept-v0"),
            log=logging.getLogger("test.voice"),
        )
        self.to_cloud = self.bus.subscribe(ConversationToCloud, maxsize=10_000)
        self.states = self.bus.subscribe(InteractionChanged)
        self.offers = self.bus.subscribe(ConversationOffered)
        self.expressions = self.bus.subscribe(ExpressionRequest)
        self.wakes = self.bus.subscribe(WakeUp)
        self.module = VoiceModule(ctx, **{**SETTINGS, **changes})

    async def sent(self) -> list[RobotPart]:
        """What the robot sent to the cloud since the last call."""
        parts = []
        while self.to_cloud.pending:
            parts.append((await self.to_cloud.get()).part)
        return parts

    async def spoken(self) -> list[RobotPart]:
        """What the robot sent since the last call, but what it heard."""
        return [part for part in await self.sent() if not isinstance(part, Speech)]

    async def state_changes(self) -> list[InteractionState]:
        """Where the conversation went since the last call."""
        states = []
        while self.states.pending:
            states.append((await self.states.get()).state)
        return states

    async def cloud_says(self, *parts: CloudPart) -> None:
        for part in parts:
            self.bus.publish(ConversationFromCloud(part))
        await settle()

    async def online(self, conversations: bool = True) -> None:
        self.bus.publish(CloudStatus(online=True, conversations=conversations))
        await settle()


async def settle() -> None:
    """Let the module handle what was just published."""
    for _ in range(10):
        await asyncio.sleep(0)


@contextlib.asynccontextmanager
async def running(robot: Robot) -> AsyncIterator[Robot]:
    await robot.module.start()
    task = asyncio.create_task(robot.module.run())
    try:
        await settle()
        yield robot
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await robot.module.stop()


async def in_a_conversation(robot: Robot) -> str:
    """Hear the wake word and some voice, have the cloud start the conversation, and
    forget what was sent; returns the conversation."""
    await robot.online()
    await robot.microphones.heard(WAKE, VOICE)
    (request, _) = await robot.sent()
    assert isinstance(request, ConversationRequest)
    await robot.cloud_says(ConversationStart(request.conversation, listen=True))
    await robot.state_changes()
    return request.conversation


async def answering(robot: Robot, conversation: str) -> None:
    """A turn, then the start of its answer."""
    await robot.cloud_says(
        TurnStart(conversation, "t1"),
        SpeechEnd(conversation, "t1"),
        ResponseStart(conversation, "a1", "t1", "vi"),
    )
    await robot.sent()
    await robot.state_changes()


def test_it_offers_conversations_and_starts_idle() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            offer = await robot.offers.get()
            assert (offer.owner, offer.audio) == ("voice", ConversationAudio(FORMAT, FORMAT))
            assert await robot.state_changes() == [InteractionState.IDLE]

    asyncio.run(main())


# ---- opening ----


def test_a_wake_word_asks_for_a_conversation_and_streams_from_then_on() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            await robot.online()
            await robot.state_changes()
            await robot.microphones.heard(SILENCE, WAKE, VOICE, SILENCE)
            request, *speech = await robot.sent()
            assert isinstance(request, ConversationRequest)
            assert request.trigger is Trigger.WAKE_WORD
            assert robot.wakes.pending == 1  # the screen wakes with it
            assert await robot.wakes.get() == WakeUp("voice", WakeReason.WAKE_WORD)
            assert uuid.UUID(request.conversation).version == 7
            conversation = request.conversation
            assert speech == [Speech(conversation, VOICE), Speech(conversation, SILENCE)]
            assert await robot.state_changes() == [InteractionState.TRIGGERED]
            assert robot.spotter.resets == 1  # the same words are not spotted twice
            await robot.cloud_says(ConversationStart(conversation, listen=True))
            assert await robot.state_changes() == [InteractionState.ATTENTIVE]

    asyncio.run(main())


def test_without_the_cloud_the_wake_word_gets_the_robots_cue() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            await robot.online(conversations=False)
            await robot.microphones.heard(WAKE, VOICE)
            assert await robot.sent() == []
            cue = await asyncio.wait_for(robot.expressions.get(), TIMEOUT)
            assert (cue.expression, cue.duration) == ("confused", 2.0)

    asyncio.run(main())


@pytest.mark.parametrize("reason", ["limit_reached", "unavailable", "no_answer"])
def test_a_conversation_that_cannot_start_gets_the_robots_cue(reason: str) -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            await robot.online()
            await robot.microphones.heard(WAKE)
            (request,) = await robot.sent()
            assert isinstance(request, ConversationRequest)
            await robot.state_changes()
            await robot.cloud_says(ConversationEnd(request.conversation, reason))
            assert await robot.state_changes() == [InteractionState.IDLE]
            assert (await asyncio.wait_for(robot.expressions.get(), TIMEOUT)).expression == (
                "confused"
            )
            await robot.microphones.heard(VOICE)
            assert await robot.sent() == []  # it streams no more

    asyncio.run(main())


def test_the_cloud_opens_a_conversation_to_listen() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            await robot.online()
            await robot.state_changes()
            await robot.cloud_says(ConversationStart(REMINDER, listen=True))
            assert await robot.state_changes() == [InteractionState.ATTENTIVE]
            await robot.microphones.heard(VOICE)
            assert await robot.sent() == [Speech(REMINDER, VOICE)]

    asyncio.run(main())


def test_a_wake_word_ends_a_conversation_that_does_not_listen_and_opens_one() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            await robot.online()
            await robot.cloud_says(
                ConversationStart(REMINDER, listen=False),
                ResponseStart(REMINDER, "a1", None, "vi"),
                Voice(REMINDER, b"\x01\x00" * 4),
            )
            await robot.microphones.heard(VOICE)
            assert await robot.sent() == []  # it only talks
            assert bytes(robot.speaker.played) == b"\x01\x00" * 4
            await robot.microphones.heard(WAKE)
            end, request = await robot.sent()
            assert end == ConversationEnd(REMINDER, "user")
            assert isinstance(request, ConversationRequest)
            assert request.conversation != REMINDER

    asyncio.run(main())


def test_a_late_start_of_a_conversation_the_robot_ended_opens_nothing() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            await robot.online()
            await robot.microphones.heard(WAKE, *chunks(2.0, SILENCE))
            request, *_, end = await robot.sent()
            assert isinstance(request, ConversationRequest)
            assert end == ConversationEnd(request.conversation, "idle")  # nothing heard
            await robot.cloud_says(ConversationStart(request.conversation, listen=True))
            await robot.microphones.heard(VOICE)
            assert await robot.sent() == []
            assert robot.module.conversation is None

    asyncio.run(main())


# ---- turns and answers ----


def test_the_clouds_turns_and_answers_show_on_the_face() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            conversation = await in_a_conversation(robot)
            await robot.cloud_says(TurnStart(conversation, "t1"))
            await robot.cloud_says(SpeechEnd(conversation, "t1"))
            await robot.cloud_says(ResponseStart(conversation, "a1", "t1", "vi"))
            await robot.cloud_says(Voice(conversation, b"\x02\x00" * 4))
            robot.speaker.playing.clear()
            await robot.cloud_says(ResponseEnd(conversation, "a1"))
            assert await robot.state_changes() == [
                InteractionState.LISTENING,
                InteractionState.PROCESSING,
                InteractionState.SPEAKING,
            ]
            robot.speaker.playing.set()  # the answer has played
            await settle()
            assert await robot.state_changes() == [InteractionState.ATTENTIVE]
            assert bytes(robot.speaker.played) == b"\x02\x00" * 4

    asyncio.run(main())


def test_it_streams_for_the_whole_conversation_even_while_it_speaks() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            conversation = await in_a_conversation(robot)
            await answering(robot, conversation)
            await robot.microphones.heard(SILENCE, VOICE)
            assert await robot.sent() == [
                Speech(conversation, SILENCE),
                Speech(conversation, VOICE),
            ]

    asyncio.run(main())


def test_without_echo_cancelling_it_streams_nothing_while_it_speaks() -> None:
    async def main() -> None:
        async with running(Robot(echo_cancelled=False)) as robot:
            conversation = await in_a_conversation(robot)
            await answering(robot, conversation)
            await robot.microphones.heard(*chunks(0.4, VOICE))  # its own voice, perhaps
            assert await robot.sent() == []  # neither streamed nor taken for a barge-in
            assert robot.speaker.drops == 0
            await robot.cloud_says(ResponseEnd(conversation, "a1"))
            await robot.microphones.heard(VOICE)
            assert await robot.sent() == [Speech(conversation, VOICE)]

    asyncio.run(main())


def test_barge_in_stops_the_answer_and_tells_the_cloud() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            conversation = await in_a_conversation(robot)
            await answering(robot, conversation)
            drops = robot.speaker.drops
            await robot.microphones.heard(*chunks(0.2, VOICE))
            assert robot.speaker.drops == drops + 1
            assert await robot.spoken() == [Interrupted(conversation, "a1")]
            assert await robot.state_changes() == [InteractionState.LISTENING]
            await robot.cloud_says(Voice(conversation, b"\x03\x00" * 4))  # still on its way
            assert robot.speaker.played == bytearray()
            await robot.cloud_says(
                ResponseStart(conversation, "a2", None, "vi"), Voice(conversation, b"\x04\x00")
            )
            assert bytes(robot.speaker.played) == b"\x04\x00"  # the next answer plays

    asyncio.run(main())


def test_a_short_noise_over_an_answer_does_not_interrupt_it() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            conversation = await in_a_conversation(robot)
            await answering(robot, conversation)
            await robot.microphones.heard(*chunks(0.1, VOICE), SILENCE, *chunks(0.1, VOICE))
            assert await robot.spoken() == []

    asyncio.run(main())


def test_a_stopped_answer_is_dropped_and_its_voice_not_played() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            conversation = await in_a_conversation(robot)
            await answering(robot, conversation)
            drops = robot.speaker.drops
            await robot.cloud_says(ResponseStop(conversation, "a1"))
            assert robot.speaker.drops == drops + 1
            assert await robot.state_changes() == [InteractionState.ATTENTIVE]
            await robot.cloud_says(Voice(conversation, b"\x05\x00"))
            assert robot.speaker.played == bytearray()

    asyncio.run(main())


def test_parts_of_another_conversation_are_ignored() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            await in_a_conversation(robot)
            await robot.cloud_says(
                ResponseStart(REMINDER, "a9", None, "vi"), Voice(REMINDER, b"\x06\x00")
            )
            assert robot.speaker.played == bytearray()
            assert await robot.state_changes() == []

    asyncio.run(main())


# ---- the limits ----


def test_nothing_heard_after_the_wake_word_ends_the_conversation() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            conversation = await in_a_conversation(robot)
            await robot.microphones.heard(*chunks(1.0, SILENCE))  # 1.0 s heard with the voice
            assert await robot.spoken() == []
            robot.module._voiced = False  # as if the voice had been nothing
            await robot.microphones.heard(*chunks(1.0, SILENCE))
            assert await robot.spoken() == [ConversationEnd(conversation, "idle")]
            assert await robot.state_changes() == [InteractionState.IDLE]

    asyncio.run(main())


def test_a_silence_of_idle_seconds_ends_the_conversation() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            conversation = await in_a_conversation(robot)
            await robot.microphones.heard(*chunks(4.98, SILENCE))
            assert await robot.spoken() == []
            await robot.microphones.heard(SILENCE)
            assert await robot.spoken() == [ConversationEnd(conversation, "idle")]
            await robot.microphones.heard(VOICE)
            assert await robot.sent() == []  # it streams no more (PRIV-1)

    asyncio.run(main())


def test_speech_the_cloud_drops_counts_as_silence() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            conversation = await in_a_conversation(robot)
            await robot.cloud_says(TurnStart(conversation, "t1"))
            await robot.microphones.heard(*chunks(2.0, VOICE))  # someone else, on the phone
            await robot.cloud_says(TurnCancel(conversation, "t1", "not_for_the_device"))
            await robot.microphones.heard(*chunks(2.98, SILENCE))
            assert await robot.spoken() == []
            await robot.microphones.heard(SILENCE)
            assert await robot.spoken() == [ConversationEnd(conversation, "idle")]

    asyncio.run(main())


def test_voice_the_cloud_has_not_judged_holds_the_silence() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            conversation = await in_a_conversation(robot)
            await robot.microphones.heard(*chunks(4.0, SILENCE), *chunks(3.0, VOICE))
            await robot.microphones.heard(*chunks(0.98, SILENCE))
            assert await robot.spoken() == []
            await robot.microphones.heard(SILENCE)
            assert await robot.spoken() == [ConversationEnd(conversation, "idle")]

    asyncio.run(main())


def test_thinking_and_speaking_hold_the_conversation_open() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            conversation = await in_a_conversation(robot)
            await robot.cloud_says(TurnStart(conversation, "t1"), SpeechEnd(conversation, "t1"))
            await robot.microphones.heard(*chunks(6.0, SILENCE))  # the cloud thinks
            await robot.cloud_says(ResponseStart(conversation, "a1", "t1", "vi"))
            robot.speaker.playing.clear()
            await robot.cloud_says(ResponseEnd(conversation, "a1"))
            await robot.microphones.heard(*chunks(6.0, SILENCE))  # the robot speaks
            assert await robot.spoken() == []
            robot.speaker.playing.set()
            await settle()
            await robot.microphones.heard(*chunks(4.98, SILENCE))
            assert await robot.spoken() == []
            await robot.microphones.heard(SILENCE)
            assert await robot.spoken() == [ConversationEnd(conversation, "idle")]

    asyncio.run(main())


def test_the_longest_conversation_ends() -> None:
    async def main() -> None:
        async with running(Robot(longest=3.0)) as robot:
            conversation = await in_a_conversation(robot)  # 0.02 s heard
            await robot.cloud_says(TurnStart(conversation, "t1"))
            await robot.microphones.heard(*chunks(2.96, VOICE))
            assert await robot.spoken() == []
            await robot.microphones.heard(VOICE)
            assert await robot.spoken() == [ConversationEnd(conversation, "longest")]

    asyncio.run(main())


def test_the_cloud_ends_the_conversation() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            conversation = await in_a_conversation(robot)
            await answering(robot, conversation)
            await robot.cloud_says(Voice(conversation, b"\x07\x00"))
            robot.speaker.playing.clear()
            await robot.cloud_says(
                ResponseEnd(conversation, "a1"), ConversationEnd(conversation, "done")
            )
            await robot.microphones.heard(VOICE)
            assert await robot.sent() == []  # it streams no more at once (PRIV-1)
            robot.speaker.playing.set()  # what was left of the answer played out
            await settle()
            assert await robot.state_changes() == [InteractionState.IDLE]
            assert bytes(robot.speaker.played) == b"\x07\x00"
            assert robot.expressions.pending == 0  # it ended well: no cue
            await robot.cloud_says(Voice(conversation, b"\x08\x00"))  # crossed the end
            assert bytes(robot.speaker.played) == b"\x07\x00"

    asyncio.run(main())


def test_a_conversation_lost_on_its_way_gets_the_robots_cue() -> None:
    async def main() -> None:
        async with running(Robot()) as robot:
            conversation = await in_a_conversation(robot)
            await robot.cloud_says(ConversationEnd(conversation, "media_lost"))
            assert await robot.state_changes() == [InteractionState.IDLE]
            assert (await asyncio.wait_for(robot.expressions.get(), TIMEOUT)).expression == (
                "confused"
            )

    asyncio.run(main())


# ---- the settings ----


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("voice_threshold", 1.5),
        ("nothing_heard", -1.0),
        ("barge_in", float("nan")),
        ("longest", 0.0),
        ("offline_expression", ""),
        ("offline_seconds", 0.0),
        ("volume", 83),  # not a step of 5
        ("volume", 120),
        ("microphones", "yes"),
        ("idle", 7),  # not a step of 5
        ("idle", 35),
        ("idle_least", 0),
        ("idle_most", 4),  # below idle_least
    ],
)
def test_settings_are_checked(setting: str, value: Any) -> None:
    with pytest.raises(ValueError, match=setting):
        Robot(**{setting: value})


def test_engines_that_take_other_audio_are_refused() -> None:
    robot = Robot()
    ports = {
        "audio_in": robot.microphones,
        "audio_out": robot.speaker,
        "wake_word": Spotter(AudioFormat(48000, 1)),
        "voice_activity": Detector(),
    }
    ctx = ModuleContext(
        "voice", Bus(), ports, RobotInfo("Robot", "robot", "concept-v0"), logging.getLogger("test")
    )
    with pytest.raises(ValueError, match="Spotter takes"):
        VoiceModule(ctx, **SETTINGS)


async def microphones(robot: Robot) -> list[bool]:
    """What the module said of its microphones since the last call."""
    said = []
    while robot.mics.pending:
        said.append((await robot.mics.get()).on)
    return said


def settings_robot(**changes: Any) -> Robot:
    robot = Robot(**changes)
    robot.mics = robot.bus.subscribe(MicrophonesChanged)
    robot.levels = robot.bus.subscribe(SoundLevel)
    robot.offered = robot.bus.subscribe(SettingOffered)
    return robot


def test_it_offers_its_settings() -> None:
    async def main() -> None:
        async with running(settings_robot(volume=60, idle=15)) as robot:
            offered = {}
            while robot.offered.pending:
                setting = (await robot.offered.get()).setting
                offered[setting.name] = setting
            assert offered["voice.volume"].value == 60
            assert offered["voice.volume"].unit == "%"
            assert offered["voice.microphones"].value is True
            idle = offered["voice.idle"]
            assert (idle.value, idle.unit) == (15, "s")
            assert idle.schema is not None
            assert (idle.schema["minimum"], idle.schema["maximum"]) == (5, 30)
            assert await microphones(robot) == [True]

    asyncio.run(main())


def test_the_owner_chooses_the_silence_that_ends_a_conversation() -> None:
    async def main() -> None:
        async with running(settings_robot()) as robot:
            robot.bus.publish(SettingChanged("voice.idle", 10))
            await settle()
            conversation = await in_a_conversation(robot)
            await robot.microphones.heard(*chunks(9.98, SILENCE))
            assert await robot.spoken() == []
            await robot.microphones.heard(SILENCE)
            assert await robot.spoken() == [ConversationEnd(conversation, "idle")]

    asyncio.run(main())


def test_microphones_off_hear_nothing() -> None:
    async def main() -> None:
        async with running(settings_robot()) as robot:
            await robot.online()
            await microphones(robot)
            robot.bus.publish(SettingChanged("voice.microphones", False))
            await settle()
            assert await microphones(robot) == [False]
            while robot.levels.pending:
                await robot.levels.get()
            await robot.microphones.heard(WAKE, VOICE)
            assert await robot.sent() == []  # no conversation
            assert robot.levels.pending == 0  # nothing measured
            assert robot.wakes.pending == 0  # nor woken
            resets = robot.spotter.resets
            robot.bus.publish(SettingChanged("voice.microphones", True))
            await settle()
            assert await microphones(robot) == [True]
            assert robot.spotter.resets == resets + 1  # what came before is forgotten
            await robot.microphones.heard(WAKE)
            (request,) = await robot.sent()
            assert isinstance(request, ConversationRequest)

    asyncio.run(main())


def test_microphones_off_end_the_conversation() -> None:
    async def main() -> None:
        async with running(settings_robot()) as robot:
            conversation = await in_a_conversation(robot)
            robot.bus.publish(SettingChanged("voice.microphones", False))
            await settle()
            assert await robot.sent() == [ConversationEnd(conversation, "muted")]
            assert await robot.state_changes() == [InteractionState.IDLE]

    asyncio.run(main())


def test_the_volume_scales_the_robots_voice() -> None:
    async def main() -> None:
        async with running(settings_robot(volume=50)) as robot:
            conversation = await in_a_conversation(robot)
            await answering(robot, conversation)
            await robot.cloud_says(Voice(conversation, b"\x10\x00" * 4))  # 16
            robot.bus.publish(SettingChanged("voice.volume", 25))
            await settle()
            await robot.cloud_says(Voice(conversation, b"\x10\x00" * 4))
            assert bytes(robot.speaker.played) == b"\x08\x00" * 4 + b"\x04\x00" * 4

    asyncio.run(main())


def test_a_value_its_schema_refuses_is_not_applied() -> None:
    async def main() -> None:
        async with running(settings_robot()) as robot:
            while robot.offered.pending:
                await robot.offered.get()
            robot.bus.publish(SettingChanged("voice.volume", 7))
            await settle()
            assert robot.module.volume == 100
            again = (await robot.offered.get()).setting  # offered again, as it is
            assert (again.name, again.value) == ("voice.volume", 100)

    asyncio.run(main())


def test_kept_settings_apply_before_it_hears_anything() -> None:
    async def main() -> None:
        robot = settings_robot()
        robot.bus.publish(SettingChanged("voice.microphones", False))  # as settings starts
        async with running(robot):
            assert await microphones(robot) == [False]  # off from the start
            await robot.online()
            await robot.microphones.heard(WAKE)
            assert await robot.sent() == []

    asyncio.run(main())


def test_the_real_engines_open_a_conversation_in_a_recording() -> None:
    """A recording, as the microphones would hear it: the wake word opens a
    conversation, and everything that follows streams."""
    data = Path(__file__).parent / "data"

    def recording(name: str) -> bytes:
        with wave.open(str(data / name)) as audio:
            return audio.readframes(audio.getnframes())

    async def main() -> None:
        robot = Robot()
        spotter, detector = MicroWakeWords(models=["okay_nabu"]), SileroVoiceActivity()
        robot.module.wake_word = spotter
        robot.module.voice_activity = detector
        await spotter.open()
        await detector.open()
        try:
            async with running(robot):
                await robot.online()
                speech = recording("speech.wav")
                heard = recording("okay_nabu.wav") + speech + bytes(2 * 32000)
                await robot.microphones.heard(
                    *(heard[at : at + CHUNK] for at in range(0, len(heard), CHUNK)),
                    timeout=ENGINES_TIMEOUT,
                )
                request, *rest = await robot.sent()
        finally:
            await spotter.close()
        assert isinstance(request, ConversationRequest)
        streamed = b"".join(part.pcm for part in rest if isinstance(part, Speech))
        assert [part for part in rest if not isinstance(part, Speech)] == []  # it goes on
        assert heard.endswith(streamed)  # from the wake word's end
        assert len(streamed) >= len(speech) + 2 * 32000

    asyncio.run(main())
