# SPDX-License-Identifier: MIT
"""Conversations over the cloud link (platform/protocols/cloud/README.md,
Conversations): asked for and ended on the control channel, held in a LiveKit room of
their own (``room.py``). The robot holds one at a time."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from ai_robot.ports.cloud import (
    CloudPart,
    ConversationEnd,
    ConversationRequest,
    ConversationStart,
    Interrupted,
    RobotPart,
    SafetyClass,
    Speech,
    check_url,
)

if TYPE_CHECKING:
    from ai_robot.adapters.websocket.cloud import WebSocketCloud

log = logging.getLogger(__name__)

# How this implementation works, which nobody needs to tune.
ENDED_KEPT = 64  # conversations remembered once over, so that what crosses their end is known


class Room(Protocol):
    """A conversation's media: what ``room.py`` makes, and what tests replace."""

    async def run(self) -> str | None:
        """Join, and hold the room until it is left: ``None``, or why it was lost."""

    async def hear(self, pcm: bytes) -> None:
        """What the robot hears."""

    async def interrupted(self, response: str) -> None:
        """The user spoke over ``response``."""

    async def leave(self) -> None:
        """Leave the room."""


@dataclass
class Conversation:
    """A conversation in progress: the robot's, from its request, or the cloud's."""

    id: str
    listen: bool = True
    started: bool = False
    request: int | None = None  # the number of the robot's conversation.request
    kept: bytearray = field(default_factory=bytearray)  # heard before it has a room
    room: Room | None = None
    task: asyncio.Task[None] | None = None  # holds the room
    timer: asyncio.TimerHandle | None = None  # start_timeout


class Conversations:
    """The conversations of ``link``: the robot's part comes through ``converse``, the
    cloud's through the session (``on_start``, ``on_end``, ``on_error``), and what the
    agents say through the room."""

    def __init__(self, link: WebSocketCloud) -> None:
        self.link = link
        self.current: Conversation | None = None
        self._ended: deque[str] = deque(maxlen=ENDED_KEPT)

    def named(self) -> str | None:
        """The conversation in progress, which a new session's hello names."""
        current = self.current
        return current.id if current is not None and current.started else None

    async def close(self) -> None:
        """The link stops: leave the room, if there is one."""
        current, self.current = self.current, None
        if current is not None:
            await self._leave(current)

    async def end(self, reason: str) -> None:
        """End the conversation in progress, if any, for ``reason``, telling the cloud
        and the robot."""
        if self.current is not None:
            await self._end(self.current, reason, tell_cloud=True, tell_robot=True)

    # ---- the robot's part ----

    async def converse(self, part: RobotPart) -> None:
        if isinstance(part, ConversationRequest):
            await self._request(part)
            return
        current = self.current
        if current is None or part.conversation != current.id:
            return  # it crossed the end of its conversation
        if isinstance(part, Speech):
            await self._hear(current, part.pcm)
        elif isinstance(part, Interrupted):
            if current.room is not None:
                await current.room.interrupted(part.response)
        elif isinstance(part, ConversationEnd):
            await self._end(current, part.reason, tell_cloud=True, tell_robot=False)

    async def _request(self, part: ConversationRequest) -> None:
        if self.current is not None:  # the robot ends one before it asks for another
            await self._end(self.current, "user", tell_cloud=True, tell_robot=False)
        if self.link.leaving:
            self._tell(ConversationEnd(part.conversation, "unavailable"))
            return
        session = self.link.session
        if session is None or self.link.connection is None:
            self._tell(ConversationEnd(part.conversation, "offline"))
            return
        if not session.conversations:
            self._tell(ConversationEnd(part.conversation, "unavailable"))
            return
        request = session.send(
            {
                "type": "conversation.request",
                "conversation": part.conversation,
                "trigger": str(part.trigger),
                "context": part.context,
            }
        )
        conversation = Conversation(part.conversation, request=request)
        loop = asyncio.get_running_loop()
        conversation.timer = loop.call_later(
            self.link.start_timeout, self._answer_late, conversation
        )
        self.current = conversation

    def _answer_late(self, conversation: Conversation) -> None:
        self._spawn(self._no_answer(conversation))

    async def _no_answer(self, conversation: Conversation) -> None:
        if self.current is conversation and not conversation.started:
            log.warning("the cloud did not answer the request for conversation %s", conversation.id)
            await self._end(conversation, "no_answer", tell_cloud=True, tell_robot=True)

    async def _hear(self, conversation: Conversation, pcm: bytes) -> None:
        if not conversation.listen:
            return
        if conversation.room is not None:
            await conversation.room.hear(pcm)
            return
        conversation.kept.extend(pcm)
        excess = len(conversation.kept) - self._most_kept()
        if excess > 0:  # the oldest go: the conversation's limits end it long before
            frame = self._frame_bytes()
            del conversation.kept[: -(-excess // frame) * frame]

    # ---- the cloud's part, on the control channel ----

    async def on_start(self, seq: int, message: Mapping[str, Any]) -> None:
        conversation_id, listen = message["conversation"], message["listen"]
        current = self.current
        if current is not None and current.id == conversation_id and not current.started:
            current.started = True
            current.listen = listen
            if current.timer is not None:
                current.timer.cancel()
            self._tell(ConversationStart(conversation_id, listen))
            await self._join(current, message["media"])
            return
        if conversation_id in self._ended:
            return  # it crossed the end of one the robot ended
        refusal = self._refusal(listen)
        if refusal is not None:
            code, text = refusal
            log.info("refusing conversation %s: %s", conversation_id, text)
            session = self.link.session
            if session is not None:
                session.error(seq, code, text)
            return
        log.info("the cloud opens conversation %s", conversation_id)
        conversation = Conversation(conversation_id, listen=listen, started=True)
        self.current = conversation
        self._tell(ConversationStart(conversation_id, listen))
        await self._join(conversation, message["media"])

    def _refusal(self, listen: bool) -> tuple[str, str] | None:
        """Why the robot refuses a conversation the cloud opens, if it does."""
        session, device = self.link.session, self.link.device
        if self.link.leaving:
            return "not_allowed", "the robot is leaving its home"
        if session is None or not session.conversations:
            return "not_allowed", "the robot holds no conversations in this session"
        if self.current is not None:
            return "busy", "the robot is in another conversation"
        if listen and (device is None or SafetyClass.PRIVACY not in device.allowed_classes):
            return "not_allowed", "the owner does not allow the cloud to listen"
        if listen and not self.link.may_listen:
            return "not_allowed", "the robot's microphones are off"
        return None

    async def on_end(self, seq: int, message: Mapping[str, Any]) -> None:
        conversation_id = message["conversation"]
        current = self.current
        if current is not None and current.id == conversation_id:
            log.info("the cloud ends conversation %s: %s", conversation_id, message["reason"])
            await self._end(current, message["reason"], tell_cloud=False, tell_robot=True)
        elif conversation_id not in self._ended:
            session = self.link.session
            if session is not None:
                text = f"conversation {conversation_id} is not active"
                session.error(seq, "unknown_conversation", text)

    async def on_error(self, message: Mapping[str, Any]) -> bool:
        """Whether the cloud's error answers the robot's request, which then fails."""
        current = self.current
        if current is None or current.started or message["reply_to"] != current.request:
            return False
        log.warning("the cloud refuses conversation %s: %s", current.id, message["code"])
        await self._end(current, message["code"], tell_cloud=False, tell_robot=True)
        return True

    # ---- the room ----

    async def _join(self, conversation: Conversation, media: Mapping[str, Any]) -> None:
        livekit = media.get("livekit")
        try:
            if not isinstance(livekit, Mapping):
                raise ValueError("the conversation's media is not LiveKit")
            url = check_url("the room's url", livekit["url"], ("wss", "ws"))
        except ValueError as error:
            log.warning("cannot join conversation %s: %s", conversation.id, error)
            await self._end(conversation, "error", tell_cloud=True, tell_robot=True)
            return
        link = self.link
        assert link.audio is not None and link.agent_schema is not None
        room = link.room_factory(
            conversation=conversation.id,
            url=url,
            token=livekit["token"],
            listen=conversation.listen,
            audio=link.audio,
            kept=bytes(conversation.kept),
            most_kept=self._most_kept(),
            schema=link.agent_schema,
            heard=lambda part: self._from_room(conversation, part),
            agent_timeout=link.agent_timeout,
            connect_timeout=link.connect_timeout,
            rate=link.rate,
            burst=link.burst,
            backlog=link.backlog,
        )
        conversation.kept = bytearray()
        conversation.room = room
        conversation.task = asyncio.create_task(
            self._hold(conversation, room), name=f"conversation {conversation.id}"
        )

    async def _hold(self, conversation: Conversation, room: Room) -> None:
        try:
            reason = await room.run()
        except Exception:  # a failure of the room ends its conversation, not the robot
            log.exception("the room of conversation %s failed", conversation.id)
            reason = "error"
        if reason is not None and self.current is conversation:
            await self._end(conversation, reason, tell_cloud=True, tell_robot=True)

    def _from_room(self, conversation: Conversation, part: CloudPart) -> None:
        if self.current is conversation:
            self._tell(part)

    # ---- ending ----

    async def _end(
        self, conversation: Conversation, reason: str, *, tell_cloud: bool, tell_robot: bool
    ) -> None:
        """End ``conversation``, telling the cloud and the robot as asked."""
        if self.current is not conversation:
            return
        self.current = None
        self._ended.append(conversation.id)
        if conversation.timer is not None:
            conversation.timer.cancel()
        session = self.link.session
        if tell_cloud and session is not None:
            message = {"type": "conversation.end", "conversation": conversation.id}
            session.send({**message, "reason": reason})
        if tell_robot:
            self._tell(ConversationEnd(conversation.id, reason))
        await self._leave(conversation)

    async def _leave(self, conversation: Conversation) -> None:
        if conversation.timer is not None:
            conversation.timer.cancel()
        if conversation.room is not None:
            await conversation.room.leave()
        task = conversation.task
        if task is not None and task is not asyncio.current_task():
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # ---- helpers ----

    def _tell(self, part: CloudPart) -> None:
        self.link.heard(part)

    def _spawn(self, work: Any) -> None:
        task = asyncio.ensure_future(work)
        task.add_done_callback(_report_failure)

    def _most_kept(self) -> int:
        """The most the robot keeps before an agent listens: as much as it may wait for
        the cloud and its agent."""
        audio = self.link.audio
        assert audio is not None
        seconds = self.link.start_timeout + self.link.agent_timeout
        return round(seconds * audio.speech.bytes_per_second)

    def _frame_bytes(self) -> int:
        audio = self.link.audio
        assert audio is not None
        return audio.speech.frame_bytes


def _report_failure(task: asyncio.Future[Any]) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("a conversation's task failed", exc_info=task.exception())
