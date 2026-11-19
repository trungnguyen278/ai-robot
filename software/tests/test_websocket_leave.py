# SPDX-License-Identifier: MIT
"""The robot leaves its home before its factory reset (platform/protocols/cloud/README.md,
``leave``; docs/decisions/0023-factory-reset.md), against the fake cloud."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import pytest
import waiting
from fake_cloud import TIMEOUT, CloudSide
from test_websocket_cloud import DEVICE, connect, reconnection, robot_and_cloud, status_where
from test_websocket_conversations import MEDIA, MINE, THEIRS, asked, rooms_of, talking, told

from ai_robot.adapters.websocket.cloud import WebSocketCloud
from ai_robot.ports import ConversationEnd, ConversationRequest, Trigger

ANOTHER = "01a18086-9160-765a-a90f-9dd0604d45f4"  # a conversation asked for while leaving


def run(main: Callable[[], Any]) -> None:
    asyncio.run(main())


def leaving(link: WebSocketCloud) -> asyncio.Task[bool]:
    return asyncio.create_task(link.leave())


async def outcome(task: asyncio.Task[bool]) -> bool:
    return await asyncio.wait_for(task, TIMEOUT)


async def asked_to_leave(side: CloudSide) -> dict[str, Any]:
    return await side.next("leave")


def test_the_robot_leaves_once_the_cloud_says_it_left() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            side, _ = await connect(link, cloud)
            task = leaving(link)
            leave = await asked_to_leave(side)
            assert leave == {"type": "leave", "seq": leave["seq"]}
            await asyncio.sleep(0.05)
            assert not task.done()  # a leave is done once the cloud says so
            cloud.robots.clear()
            await side.send({"type": "bye", "reason": "left"})
            assert await outcome(task) is True
            await status_where(link, online=False, bound=False)

    run(main)


@pytest.mark.parametrize("how", ["revoked", "invalid_client"])
def test_a_removal_meanwhile_or_a_refused_token_ends_the_leave(how: str) -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            side, _ = await connect(link, cloud)
            task = leaving(link)
            await asked_to_leave(side)
            cloud.robots.clear()  # the cloud removed the robot; its answer is lost
            if how == "revoked":
                await side.send({"type": "bye", "reason": "revoked"})
            else:
                await side.close()
            assert await outcome(task) is True

    run(main)


def test_a_robot_its_server_does_not_know_is_in_no_home() -> None:
    """It knows so from the server's last answer, and asks nothing more: offline too."""

    async def main() -> None:
        async with robot_and_cloud(known=False) as (link, cloud):
            await link.describe(DEVICE)
            await link.offer([], [])
            await status_where(link, bound=False)
            await cloud.stop()
            assert await outcome(leaving(link)) is True

    run(main)


def test_a_cloud_that_fails_to_remove_the_robot_is_asked_again() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            side, _ = await connect(link, cloud)
            task = leaving(link)
            first = await asked_to_leave(side)
            error = {"reply_to": first["seq"], "code": "internal", "message": "try again"}
            await side.send({"type": "error", **error})
            second = await asked_to_leave(side)
            assert second["seq"] > first["seq"]
            assert not task.done()
            await side.send({"type": "bye", "reason": "left"})
            assert await outcome(task) is True

    run(main)


def test_an_older_cloud_gets_no_leave_and_the_robot_goes_on() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            side, _ = await connect(link, cloud, protocol="3.1")
            started = time.monotonic()
            assert await outcome(leaving(link)) is False
            assert time.monotonic() - started < 1.0  # at once, not after leave_timeout
            await asyncio.sleep(0.05)
            assert [m["type"] for m in side.pending()] == []

    run(main)


def test_without_an_outcome_the_robot_gives_up_after_leave_timeout() -> None:
    async def main() -> None:
        async with robot_and_cloud(leave_timeout=0.3) as (link, cloud):
            side, _ = await connect(link, cloud)
            started = time.monotonic()
            task = leaving(link)
            await asked_to_leave(side)
            assert await outcome(task) is False
            assert time.monotonic() - started >= 0.25  # 0.3 s, as clocks on Windows tick

    run(main)


@pytest.mark.parametrize("resumed", [True, False])
def test_a_lost_answer_brings_the_leave_again_without_the_backoff(resumed: bool) -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            side, _ = await connect(link, cloud)
            link._backoff = lambda *_, **__: 30.0  # type: ignore[method-assign]
            task = leaving(link)
            first = await asked_to_leave(side)
            side, hello = await reconnection(cloud, side)  # at once: no backoff
            if resumed:  # the cloud did not receive the leave: the robot sends it again
                await side.hello(resumed=True, received=first["seq"] - 1)
                again = await asked_to_leave(side)
                assert again == first
            else:  # a new session: a new leave
                assert hello["resume"]["session"]
                await side.hello(session="01a18085-942c-78a0-9282-31c8cede2443")
                await asked_to_leave(side)
            await side.send({"type": "bye", "reason": "left"})
            assert await outcome(task) is True

    run(main)


def test_a_resumed_session_that_received_the_leave_does_not_send_it_twice() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            side, _ = await connect(link, cloud)
            task = leaving(link)
            first = await asked_to_leave(side)
            side, _ = await reconnection(cloud, side)
            await side.hello(resumed=True, received=first["seq"])
            await asyncio.sleep(0.1)
            assert [m["type"] for m in side.pending()] == []
            await side.send({"type": "bye", "reason": "left"})
            assert await outcome(task) is True

    run(main)


def test_an_offline_robot_tries_at_once_but_keeps_to_retry_after() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            link._backoff = lambda *_, **__: 30.0  # type: ignore[method-assign]
            cloud.refuse = 503
            cloud.refuse_headers = {"Retry-After": "1"}
            await link.describe(DEVICE)
            await link.offer([], [])
            await waiting.until(lambda: cloud.refusals == 1, TIMEOUT)
            refused = time.monotonic()
            cloud.refuse = None
            task = leaving(link)
            side = await cloud.connection()
            waited = time.monotonic() - refused
            assert 0.9 <= waited < 5.0  # the Retry-After, not the backoff
            await side.next("hello")
            await side.hello()
            await asked_to_leave(side)
            await side.send({"type": "bye", "reason": "left"})
            assert await outcome(task) is True

    run(main)


def test_leaving_ends_the_conversation_first_and_holds_no_other() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            rooms_of(link)
            side, _ = await talking(link, cloud)
            request = await asked(link, side)
            start = {"conversation": MINE, "listen": True, "media": MEDIA}
            await side.send({"type": "conversation.start", "reply_to": request["seq"], **start})
            await told(link)  # it starts
            task = leaving(link)
            end = await side.next()
            assert (end["type"], end["conversation"], end["reason"]) == (
                "conversation.end",
                MINE,
                "user",
            )
            await asked_to_leave(side)
            assert await told(link) == ConversationEnd(MINE, "user")
            await link.converse(ConversationRequest(ANOTHER, Trigger.WAKE_WORD, {}))
            ended = await told(link)
            assert isinstance(ended, ConversationEnd) and ended.reason == "unavailable"
            start = {"conversation": THEIRS, "listen": False, "media": MEDIA}
            sent = await side.send({"type": "conversation.start", **start})
            error = await side.next("error")
            assert (error["reply_to"], error["code"]) == (sent["seq"], "not_allowed")
            await side.send({"type": "bye", "reason": "left"})
            assert await outcome(task) is True

    run(main)
