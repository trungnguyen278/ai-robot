# SPDX-License-Identifier: MIT
"""Voice: listening and talking (docs/decisions/0013 and 0018)."""

from __future__ import annotations

import asyncio
from collections import deque

from ai_robot import native
from ai_robot.core.checks import positive, within
from ai_robot.core.ids import uuid7
from ai_robot.core.module import Module, ModuleContext
from ai_robot.core.settings import SettingsHost
from ai_robot.messages import (
    CloudStatus,
    ConversationFromCloud,
    ConversationOffered,
    ConversationToCloud,
    ExpressionRequest,
    InteractionChanged,
    InteractionState,
    MicrophonesChanged,
    Setting,
    SettingKind,
    SettingSection,
    SoundLevel,
    WakeReason,
    WakeUp,
)
from ai_robot.ports import (
    AudioIn,
    AudioOut,
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

# Fixed by the protocol (platform/protocols/cloud/README.md, Ending): the reasons a
# conversation ends as it should. After any other, the robot gives its cue.
ENDED_WELL = frozenset({"done", "user", "idle", "longest", "muted"})

# How this implementation works, which nobody needs to tune.
FROM_CLOUD_BACKLOG = 1000  # unread parts of the cloud's conversation kept: far more than pile up
VOLUME_STEP = 5  # percent between two volumes the settings offer
IDLE_STEP = 5  # seconds between two silences the settings offer
ENDED_KEPT = 16  # conversations remembered once over, so that a late start opens none


class VoiceModule(Module):
    """Listens and talks: it holds the robot's conversations with the cloud
    (docs/decisions/0018).

    - **Opening.** A wake word opens a conversation, if the cloud takes them. If it does
      not, or the conversation cannot start, the face shows ``offline_expression`` for
      ``offline_seconds``: the robot's cue, without words. The cloud may open one too,
      to listen or only to talk; a wake word ends one that does not listen, and opens
      another.
    - **Listening.** The robot streams what it hears for the whole conversation, the
      face showing it. Unless the microphones cancel the echo of what the robot plays
      (``echo_cancelled``, a voice processor's), it streams nothing while it speaks.
    - **Turns** are the cloud's: the face shows when the user speaks, when the robot
      thinks and when it speaks, and that it is attentive between turns.
    - **Answers** play as they come. With the echo cancelled, ``barge_in`` seconds of
      voice over an answer stop it at once, and the cloud hears of it; it already has
      the words.
    - **Limits.** The robot ends a conversation after ``nothing_heard`` seconds without
      voice from its opening, as after a false wake; after ``idle`` seconds in which
      nobody speaks to it and it does not speak; after ``longest`` seconds; and when its
      microphones go off. Speech the cloud drops as not meant for the robot counts as
      silence.
    - **Voice** is a window whose probability of voice reaches ``voice_threshold``.
      Durations are measured in the audio heard, to the byte, not on a clock.

    It publishes where the conversation is (``InteractionChanged``), and the loudness
    of what the microphones hear (``SoundLevel``).

    **Settings** (docs/decisions/0016): the ``volume`` of the robot's voice, in percent;
    whether the ``microphones`` are on (off, the robot hears nothing, and the screen
    says so: ``MicrophonesChanged``, PRIV-1); and ``idle``, which trades privacy for
    ease, from ``idle_least`` to ``idle_most`` seconds.
    """

    owns = ("audio_in", "audio_out", "wake_word", "voice_activity")

    def __init__(
        self,
        ctx: ModuleContext,
        *,
        voice_threshold: float,
        nothing_heard: float,
        barge_in: float,
        longest: float,
        offline_expression: str,
        offline_seconds: float,
        volume: int,
        microphones: bool,
        idle: int,
        idle_least: int,
        idle_most: int,
    ) -> None:
        super().__init__(ctx)
        self.microphones = self.port("audio_in", AudioIn)
        self.speaker = self.port("audio_out", AudioOut)
        self.wake_word = self.port("wake_word", WakeWord)
        self.voice_activity = self.port("voice_activity", VoiceActivity)
        self.voice_threshold = within("voice_threshold", voice_threshold, 0, 1)
        self.nothing_heard = positive("nothing_heard", nothing_heard)
        self.barge_in = positive("barge_in", barge_in)
        self.longest = positive("longest", longest)
        if not offline_expression:
            raise ValueError("offline_expression must name an expression")
        self.offline_expression = offline_expression
        self.offline_seconds = positive("offline_seconds", offline_seconds)
        if positive("idle_least", idle_least) > idle_most:
            raise ValueError(f"idle_most must not be below idle_least, got {idle_most}")
        self.volume = volume
        self.microphones_on = microphones
        self._said_microphones = False  # whether it told the others yet
        self.settings = SettingsHost(self)
        self.settings.add(
            Setting(
                "voice.volume",
                SettingKind.VALUE,
                SettingSection.SOUND,
                "Volume",
                volume,
                schema={"type": "integer", "minimum": 0, "maximum": 100, "multipleOf": VOLUME_STEP},
                unit="%",
            ),
            self._set_volume,
        )
        self.settings.add(
            Setting(
                "voice.microphones",
                SettingKind.VALUE,
                SettingSection.SOUND,
                "Microphones",
                microphones,
                schema={"type": "boolean"},
                help="Off, the robot hears nothing until they are on again",
            ),
            self._set_microphones,
        )
        self.settings.add(
            Setting(
                "voice.idle",
                SettingKind.VALUE,
                SettingSection.SOUND,
                "End a conversation after",
                idle,
                schema={
                    "type": "integer",
                    "minimum": idle_least,
                    "maximum": idle_most,
                    "multipleOf": IDLE_STEP,
                },
                unit="s",
                help="Seconds in which nobody speaks to the robot",
            ),
            self._set_idle,
        )
        for port in (self.wake_word, self.voice_activity):
            if port.format != self.microphones.format:
                raise ValueError(
                    f"the microphones give {self.microphones.format}, "
                    f"but {type(port).__name__} takes {port.format}"
                )
        self.echo_cancelled = self.microphones.echo_cancelled
        self.rate = self.microphones.format.bytes_per_second
        # The durations, in bytes of audio heard, which add up exactly.
        self._nothing_heard = self._bytes(self.nothing_heard)
        self._barge_in = self._bytes(self.barge_in)
        self._longest = self._bytes(self.longest)
        self._idle = self._bytes(idle)
        self.status = self.bus.subscribe(CloudStatus)
        self.from_cloud = self.bus.subscribe(ConversationFromCloud, maxsize=FROM_CLOUD_BACKLOG)
        self.conversations = False  # whether the cloud takes conversations now
        self.state = InteractionState.IDLE
        # The conversation, while there is one.
        self.conversation: str | None = None
        self.started = False  # the cloud accepted it, or opened it
        self.listen = False  # the robot streams what it hears
        self.turn: str | None = None  # the cloud's turn, until its answer starts or it drops it
        self.thinking = False  # the user has finished speaking: an answer is coming
        self.response: str | None = None  # the answer playing
        self._muted = False  # drop the cloud's voice: its answer was stopped or interrupted
        self._finishing: asyncio.Task[None] | None = None  # plays out an answer that ended
        self._ended: deque[str] = deque(maxlen=ENDED_KEPT)
        # What was heard in it, in bytes of audio.
        self._heard = 0  # since it opened
        self._voiced = False  # whether voice was heard since
        self._quiet = 0  # since someone last spoke to the robot, or it spoke
        self._turn_heard = 0  # since the turn started
        self._voice_run = 0  # voice heard over the answer, without a break

    # ---- lifecycle ----

    async def start(self) -> None:
        audio = ConversationAudio(self.microphones.format, self.speaker.format)
        self.bus.publish(ConversationOffered(self.name, audio))
        self.bus.publish(InteractionChanged(self.state))
        self.settings.offer()

    async def run(self) -> None:
        await self.settings.apply_pending()  # the owner's choices, before anything is heard
        self.bus.publish(MicrophonesChanged(self.microphones_on))
        self._said_microphones = True
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self._listen())
            tasks.create_task(self._hear_the_cloud())
            tasks.create_task(self._follow_status())
            tasks.create_task(self.settings.serve())

    async def stop(self) -> None:
        self.status.close()
        self.from_cloud.close()
        self.settings.close()
        self._cancel_finishing()
        await self.speaker.drop()

    # ---- what the microphones hear ----

    async def _listen(self) -> None:
        while True:
            chunk = await self.microphones.read()
            if not self.microphones_on:
                continue  # off: nothing is heard, nor measured
            self.bus.publish(SoundLevel(native.rms_int16(chunk)))
            # The engines hear everything, so that their state follows the audio.
            voiced = await self.voice_activity.voice(chunk) >= self.voice_threshold
            wake_word = await self.wake_word.spot(chunk)
            if wake_word is not None:
                self.wake_word.reset()  # so that the same words are not spotted twice
            if wake_word is not None and not (self.conversation is not None and self.listen):
                self.log.info("heard %r", wake_word)
                self.bus.publish(WakeUp(self.name, WakeReason.WAKE_WORD))
                await self._open(Trigger.WAKE_WORD)
            elif self.conversation is not None:
                await self._hear(chunk, voiced)

    async def _hear(self, chunk: bytes, voiced: bool) -> None:
        """A chunk heard in the conversation: stream it, watch for a barge-in, and keep
        the conversation within its limits."""
        assert self.conversation is not None
        size = len(chunk)
        self._heard += size
        speaking = self._speaking()
        if self.listen:
            self._voiced = self._voiced or voiced
            if self.echo_cancelled or not speaking:
                self._send(Speech(self.conversation, chunk))
        if self.turn is not None:
            self._turn_heard += size
        if speaking:
            self._quiet = 0
            if self.listen and self.echo_cancelled and self.response is not None:
                await self._watch_for_barge_in(voiced, size)
        elif not ((self.listen and voiced) or self.turn is not None):
            # Nobody speaks to the robot, which does not speak. A turn holds the silence
            # until it is answered, the robot thinking, or dropped.
            self._quiet += size
        if self._heard >= self._longest:
            await self._end("longest")
        elif self.listen and not self._voiced and self._heard >= self._nothing_heard:
            self.log.info("nothing heard in conversation %s", self.conversation)
            await self._end("idle")
        elif self._quiet >= self._idle:
            await self._end("idle")

    async def _watch_for_barge_in(self, voiced: bool, size: int) -> None:
        """Voice over the answer: ``barge_in`` of it stops the answer at once (PERF-3).
        The stream goes on, so the cloud already has the words that interrupted."""
        self._voice_run = self._voice_run + size if voiced else 0
        if self._voice_run < self._barge_in:
            return
        assert self.conversation is not None and self.response is not None
        self.log.info("barge-in over response %s", self.response)
        interrupted = self.response
        await self._stop_answer()
        self._send(Interrupted(self.conversation, interrupted))
        self._set_state(InteractionState.LISTENING)

    # ---- opening and ending ----

    async def _open(self, trigger: Trigger) -> None:
        """Ask the cloud for a conversation, and listen from now on."""
        if self.conversation is not None:  # one that does not listen: the user ends it
            await self._end("user")
        await self._stop_answer()  # what is left of one that ended
        if not self.conversations:
            self.log.info("the cloud takes no conversations now")
            self._cue()
            return
        self.conversation = uuid7()
        self.started = False
        self.listen = True
        self._reset()
        self._send(ConversationRequest(self.conversation, trigger))
        self._set_state(InteractionState.TRIGGERED)

    def _on_start(self, part: ConversationStart) -> None:
        if part.conversation == self.conversation:  # the one the robot asked for
            self.started = True
            self.listen = part.listen
            if self.state is InteractionState.TRIGGERED:
                self._set_state(self._at_rest())
            return
        if part.conversation in self._ended:
            return  # the robot ended it before it started
        if self.conversation is not None:
            self.log.warning("conversation %s starts while another is open", part.conversation)
            return
        purpose = "to listen" if part.listen else "to talk"
        self.log.info("the cloud opens conversation %s %s", part.conversation, purpose)
        self.conversation = part.conversation
        self.started = True
        self.listen = part.listen
        self._reset()
        self._set_state(self._at_rest())

    async def _end(self, reason: str) -> None:
        """The robot ends the conversation."""
        assert self.conversation is not None
        self.log.info("ending conversation %s: %s", self.conversation, reason)
        self._send(ConversationEnd(self.conversation, reason))
        self._forget()
        await self._stop_answer()
        self._set_state(InteractionState.IDLE)

    def _on_end(self, part: ConversationEnd) -> None:
        """The conversation ended, or could not start. What is left of an answer plays
        out."""
        self.log.info("conversation %s ended: %s", part.conversation, part.reason)
        cue = not self.started or part.reason not in ENDED_WELL
        self._forget()
        if self.response is not None and self._finishing is None:
            self._finish_once_played()  # its end will not come
        if self._finishing is None:
            self._set_state(InteractionState.IDLE)
        if cue:
            self._cue()

    def _forget(self) -> None:
        if self.conversation is not None:
            self._ended.append(self.conversation)
        self.conversation = None
        self.started = False
        self.listen = False
        self.turn = None
        self.thinking = False

    def _reset(self) -> None:
        """What a new conversation has heard: nothing yet."""
        self.turn = None
        self.thinking = False
        self._heard = 0
        self._voiced = False
        self._quiet = 0
        self._turn_heard = 0
        self._voice_run = 0

    def _cue(self) -> None:
        """The robot's cue that it cannot talk now: a face, without words."""
        self.bus.publish(
            ExpressionRequest(self.name, self.offline_expression, duration=self.offline_seconds)
        )

    # ---- what the cloud says ----

    async def _hear_the_cloud(self) -> None:
        while True:
            part = (await self.from_cloud.get()).part
            if isinstance(part, ConversationStart):
                self._on_start(part)
            elif part.conversation != self.conversation:
                continue  # it crossed the end of its conversation
            elif isinstance(part, Voice):
                if not self._muted:
                    await self.speaker.write(self._at_volume(part.pcm))
            elif isinstance(part, TurnStart):
                self.turn = part.turn
                self.thinking = False
                self._turn_heard = 0
                if not self._speaking():
                    self._set_state(InteractionState.LISTENING)
            elif isinstance(part, SpeechEnd):
                if part.turn == self.turn:
                    self.thinking = True
                    self._set_state(InteractionState.PROCESSING)
            elif isinstance(part, TurnCancel):
                if part.turn == self.turn:
                    self.log.info("the cloud drops turn %s: %s", part.turn, part.reason)
                    self.turn = None
                    self.thinking = False
                    self._quiet += self._turn_heard  # it was not meant for the robot
                    if not self._speaking():
                        self._set_state(self._at_rest())
            elif isinstance(part, ResponseStart):
                self._on_response(part)
            elif isinstance(part, ResponseEnd):
                if part.response == self.response:
                    self._finish_once_played()
            elif isinstance(part, ResponseStop):
                if part.response == self.response:
                    await self._stop_answer()
                    self._set_state(self._at_rest())
            elif isinstance(part, ConversationEnd):
                self._on_end(part)

    def _on_response(self, part: ResponseStart) -> None:
        self._cancel_finishing()  # a new answer ends the one before
        self.response = part.response
        self._muted = False
        self._voice_run = 0
        self.thinking = False
        if part.turn is None or part.turn == self.turn:
            self.turn = None
        self._quiet = 0
        self._set_state(InteractionState.SPEAKING)

    def _finish_once_played(self) -> None:
        """Once the answer has been played, go back to what the conversation is."""

        async def finish() -> None:
            await self.speaker.drain()
            self._finishing = None
            self.response = None
            self._quiet = 0  # the robot has just spoken
            self._set_state(self._at_rest())

        self._finishing = asyncio.create_task(finish(), name="voice: finishing an answer")
        self._finishing.add_done_callback(self._report_failure)

    async def _stop_answer(self) -> None:
        """Stop the answer playing at once, if one plays, and drop the rest of its voice."""
        if not self._speaking():
            return
        self._cancel_finishing()
        if self.response is not None:
            self._muted = True
        self.response = None
        self._voice_run = 0
        await self.speaker.drop()

    def _speaking(self) -> bool:
        """Whether an answer plays, or plays out."""
        return self.response is not None or self._finishing is not None

    def _at_rest(self) -> InteractionState:
        """Where the conversation is between its turns and answers."""
        if self.conversation is None:
            return InteractionState.IDLE
        if self.thinking:
            return InteractionState.PROCESSING
        if self.turn is not None:
            return InteractionState.LISTENING
        return InteractionState.ATTENTIVE if self.listen else InteractionState.IDLE

    async def _follow_status(self) -> None:
        while True:
            status = await self.status.get()
            self.conversations = status.online and status.conversations

    # ---- settings ----

    async def _set_volume(self, volume: int) -> None:
        self.volume = volume

    async def _set_idle(self, idle: int) -> None:
        self._idle = self._bytes(idle)

    async def _set_microphones(self, on: bool) -> None:
        if on == self.microphones_on:
            return
        self.microphones_on = on
        if on:
            # What was heard before they went off is no part of what comes now.
            self.wake_word.reset()
            self.voice_activity.reset()
            self.log.info("the microphones are on")
        else:
            self.log.info("the microphones are off")
            if self.conversation is not None and self.listen:
                await self._end("muted")
        if self._said_microphones:
            self.bus.publish(MicrophonesChanged(on))

    def _at_volume(self, pcm: bytes) -> bytes:
        return pcm if self.volume == 100 else native.scale_int16(pcm, self.volume / 100)

    # ---- helpers ----

    def _bytes(self, seconds: float) -> int:
        """``seconds`` of the microphones' audio, in whole frames' bytes."""
        frame = self.microphones.format.frame_bytes
        return round(seconds * self.rate / frame) * frame

    def _send(self, part: RobotPart) -> None:
        self.bus.publish(ConversationToCloud(part))

    def _set_state(self, state: InteractionState) -> None:
        if state != self.state:
            self.state = state
            self.bus.publish(InteractionChanged(state))

    def _cancel_finishing(self) -> None:
        if self._finishing is not None:
            self._finishing.cancel()
            self._finishing = None

    def _report_failure(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            self.log.error("finishing an answer failed", exc_info=task.exception())
