# SPDX-License-Identifier: MIT
"""The link to ai-cloud over a WebSocket (platform/protocols/cloud/README.md), against a
fake cloud whose side each test plays. The fake checks every message the robot sends
against the protocol's schema, and its numbering."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Any, ClassVar, cast

import pytest
import waiting
from aiohttp import WSMsgType
from fake_cloud import TIMEOUT, CloudSide, FakeCloud, schema

from ai_robot.adapters.sim.identity import SimIdentity
from ai_robot.adapters.websocket import cloud as link_module
from ai_robot.adapters.websocket import mcp as mcp_module
from ai_robot.adapters.websocket.cloud import WebSocketCloud
from ai_robot.adapters.websocket.connection import MAX_BYE, Connection, Ending
from ai_robot.adapters.websocket.retry import retry_after
from ai_robot.adapters.websocket.session import MAX_MESSAGE, MAX_WARNED
from ai_robot.ports import (
    CallCancelled,
    Capability,
    ConversationEnd,
    ConversationRequest,
    DeviceInfo,
    EventKind,
    LinkStatus,
    SafetyClass,
    ToolCall,
    Trigger,
)

SWITCH = {
    "type": "object",
    "properties": {"on": {"type": "boolean"}},
    "required": ["on"],
    "additionalProperties": False,
}
LAMP = Capability(
    "lamp.set", "Turn the lamp on or off", SWITCH, SafetyClass.INFORMATIONAL, output_schema=SWITCH
)
FAN = Capability("fan.set", "Turn the fan on or off", SWITCH, SafetyClass.PHYSICAL, version=2)
CHANGED = EventKind("lamp.changed", "The lamp changed", SWITCH)
TOUCHED = EventKind("screen.touched", "Someone touched the screen")
DEVICE = DeviceInfo(
    "robot", "concept-v0", ("vi", "en"), "Asia/Ho_Chi_Minh", {SafetyClass.INFORMATIONAL}
)
SESSION = "01a18085-942c-78a0-9282-31c8cede2442"
OTHER_SESSION = "01a18085-942c-78a0-9282-31c8cede2443"

# The link's settings, quicker than shipped so that the tests take little time.
SETTINGS: dict[str, Any] = {
    "connect_timeout": 5.0,
    "hello_timeout": 1.0,
    "reconnect_first": 0.01,
    "reconnect_max": 0.05,
    "replay": 1000,
    "rate": 1000.0,
    "burst": 1000,
    "backlog": 500,
    "catalogue_ttl": 60.0,
    "waiting_events": 100,
    "start_timeout": 1.0,
    "agent_timeout": 1.0,
    "leave_timeout": 5.0,
}


@contextlib.asynccontextmanager
async def robot_and_cloud(
    known: bool = True, **settings: Any
) -> AsyncIterator[tuple[WebSocketCloud, FakeCloud]]:
    """A link and the fake cloud it connects to, which knows the robot if ``known``."""
    identity = SimIdentity()
    await identity.open()
    robots = {await identity.robot_id(): await identity.public_key()} if known else {}
    cloud = FakeCloud(robots)
    await cloud.start()
    link = WebSocketCloud(server=cloud.url, **{**SETTINGS, **settings})
    await link.identify(identity)
    await link.open()
    try:
        yield link, cloud
    finally:
        await link.close()
        await cloud.stop()
    assert cloud.problems == []


async def connect(
    link: WebSocketCloud,
    cloud: FakeCloud,
    *,
    capabilities: Sequence[Capability] = (LAMP,),
    kinds: Sequence[EventKind] = (CHANGED,),
    **hello: Any,
) -> tuple[CloudSide, dict[str, Any]]:
    """Say what the robot is and offers, take its connection and its hello, and open a
    session with ``hello``'s fields. Returns the cloud's side, and the robot's hello."""
    await link.describe(DEVICE)
    await link.offer(list(capabilities), list(kinds))
    side = await cloud.connection()
    said = await side.next("hello")
    await side.hello(**hello)
    await status_where(link, online=True)
    return side, said


async def status_where(link: WebSocketCloud, **fields: Any) -> LinkStatus:
    """The link's next status with ``fields``; the others are skipped."""
    async with asyncio.timeout(TIMEOUT):
        while True:
            status = await link.status()
            if all(getattr(status, name) == value for name, value in fields.items()):
                return status


async def reconnection(cloud: FakeCloud, side: CloudSide) -> tuple[CloudSide, dict[str, Any]]:
    """Lose ``side``'s connection; returns the next one, and the robot's hello on it."""
    await side.close()
    side = await cloud.connection()
    return side, await side.next("hello")


async def request(link: WebSocketCloud) -> ToolCall | CallCancelled:
    return await asyncio.wait_for(link.request(), TIMEOUT)


async def until(condition: Any) -> None:
    await waiting.until(condition, TIMEOUT)


def run(main: Any) -> None:
    asyncio.run(main())


# ---- connecting ----


def test_the_robot_connects_with_a_token_and_says_what_it_is() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            _, said = await connect(link, cloud, events=("lamp.changed", "unknown"))
            (token,) = cloud.tokens
            assert cloud.bearers == [f"Bearer {token}"]
            assert cloud.assertions[0]["aud"] == cloud.endpoint
            assert said["protocol"] == "3.2"
            assert said["device"]["kind"] == "robot" and said["device"]["model"] == "concept-v0"
            assert said["languages"] == ["vi", "en"] and said["time_zone"] == "Asia/Ho_Chi_Minh"
            assert said["allowed_classes"] == ["informational"]
            assert (said["conversations"], said["media"]) == (False, [])
            assert said["events"] == [
                {"name": "lamp.changed", "description": "The lamp changed", "schema": SWITCH}
            ]
            assert "resume" not in said
            assert link.session is not None
            assert link.session.events == {"lamp.changed"}  # only what it declared
            assert (link.bound, link.update_required) == (True, False)

    run(main)


def test_the_link_waits_to_know_what_the_robot_is() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            await link.offer([LAMP], [])
            with pytest.raises(TimeoutError):
                await cloud.connection(timeout=0.3)
            await link.describe(DEVICE)
            side = await cloud.connection()
            await side.next("hello")

    run(main)


def test_a_robot_the_cloud_does_not_know_must_be_set_up() -> None:
    async def main() -> None:
        async with robot_and_cloud(known=False) as (link, _):
            await link.describe(DEVICE)
            await link.offer([], [])
            await status_where(link, online=False, bound=False)

    run(main)


@pytest.mark.parametrize(
    ("server", "allowed"),
    [
        ("https://cloud.example", True),
        ("http://127.0.0.1:8080", True),
        ("http://localhost:8080/", True),
        ("http://cloud.example", False),
        ("wss://cloud.example", False),
        ("", False),
    ],
)
def test_the_servers_address_is_checked(server: str, allowed: bool) -> None:
    async def main() -> None:
        if allowed:
            link = WebSocketCloud(server=server, **SETTINGS)
            assert link.server == server
            await link.serve("https://other.example")
            assert link.server == "https://other.example"
        else:
            with pytest.raises(ValueError, match="server"):
                WebSocketCloud(server=server, **SETTINGS)

    run(main)


def test_the_control_channels_address() -> None:
    assert link_module._link_url("https://cloud.example/") == "wss://cloud.example/device/v3/link"
    assert link_module._link_url("http://127.0.0.1:8080") == "ws://127.0.0.1:8080/device/v3/link"


# ---- capabilities ----


def test_the_cloud_lists_and_calls_the_capabilities() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            side, _ = await connect(link, cloud, capabilities=(LAMP, FAN))
            asked = await side.mcp("server/discover")
            found = (await side.next("mcp"))["payload"]["result"]
            assert found["supportedVersions"] == ["2026-07-28"]
            asked = await side.mcp("tools/list", request="list")
            answer = await side.next("mcp")
            assert answer["reply_to"] == asked["seq"]
            tools = answer["payload"]["result"]["tools"]
            assert [tool["name"] for tool in tools] == ["fan.set", "lamp.set"]
            assert tools[0]["_meta"] == {"ai-platform/safety": "physical", "ai-platform/version": 2}
            asked = await side.mcp("tools/call", {"name": "lamp.set", "arguments": {"on": True}})
            call = await request(link)
            assert isinstance(call, ToolCall) and call.name == "lamp.set"
            await link.reply(call, data={"on": True})
            answer = await side.next("mcp")
            assert answer["reply_to"] == asked["seq"]
            result = answer["payload"]["result"]
            assert (result["structuredContent"], result["isError"]) == ({"on": True}, False)
            assert json.loads(result["content"][0]["text"]) == {"on": True}
            await side.mcp("tools/call", {"name": "lamp.set", "arguments": {"on": 3}}, request=2)
            call = await request(link)
            assert isinstance(call, ToolCall)
            await link.reply(call, error="invalid arguments")
            result = (await side.next("mcp"))["payload"]["result"]
            assert result["isError"] is True
            assert result["content"] == [{"type": "text", "text": "invalid arguments"}]

    run(main)


@pytest.mark.parametrize(
    ("method", "params", "code"),
    [
        ("tools/call", {"name": "unknown"}, -32602),
        ("tools/list", {"cursor": "next"}, -32602),
        ("prompts/list", {}, -32601),
    ],
)
def test_requests_the_robot_cannot_answer(method: str, params: dict[str, Any], code: int) -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            side, _ = await connect(link, cloud)
            await side.mcp(method, params)
            assert (await side.next("mcp"))["payload"]["error"]["code"] == code

    run(main)


def test_requests_must_carry_mcps_metadata() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            side, _ = await connect(link, cloud)
            payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
            await side.send({"type": "mcp", "payload": payload})
            assert (await side.next("mcp"))["payload"]["error"]["code"] == -32602
            old = {
                "io.modelcontextprotocol/protocolVersion": "2025-06-18",
                "io.modelcontextprotocol/clientCapabilities": {},
            }
            payload = {**payload, "params": {"_meta": old}}
            await side.send({"type": "mcp", "payload": payload})
            error = (await side.next("mcp"))["payload"]["error"]
            assert error["code"] == -32022
            assert error["data"] == {"supported": ["2026-07-28"], "requested": "2025-06-18"}

    run(main)


def test_subscribers_hear_of_a_new_catalogue_and_calls_can_be_cancelled() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            side, _ = await connect(link, cloud)
            asked = await side.mcp(
                "subscriptions/listen", {"notifications": {"toolsListChanged": True}}, request="s"
            )
            acknowledged = await side.next("mcp")
            assert acknowledged["reply_to"] == asked["seq"]
            assert acknowledged["payload"]["method"] == "notifications/subscriptions/acknowledged"
            await link.offer([LAMP, FAN], [CHANGED])
            changed = (await side.next("mcp"))["payload"]
            assert changed["method"] == "notifications/tools/list_changed"
            await side.mcp("tools/call", {"name": "lamp.set", "arguments": {"on": True}}, request=5)
            call = await request(link)
            assert isinstance(call, ToolCall)
            cancel = {"jsonrpc": "2.0", "method": "notifications/cancelled"}
            await side.send({"type": "mcp", "payload": {**cancel, "params": {"requestId": 5}}})
            assert await request(link) == CallCancelled(call.id)
            await link.reply(call, data={"on": True})  # too late: nothing goes
            await link.close()
            ended = await side.next("mcp")  # the subscription ends with the session
            assert ended["reply_to"] == asked["seq"]
            assert ended["payload"]["result"]["resultType"] == "complete"
            assert await side.next("bye") == {"type": "bye", "reason": "stopping"}

    run(main)


# ---- events and the owner's choices ----


def test_events_go_only_where_the_cloud_subscribed() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            side, _ = await connect(link, cloud, events=("lamp.changed",))
            before = time.time()
            await link.emit("screen.touched", {})  # not subscribed
            await link.emit("lamp.changed", {"on": True})
            event = await side.next("event")
            assert (event["name"], event["data"]) == ("lamp.changed", {"on": True})
            assert event["time"].endswith("Z")  # RFC 3339, in UTC
            assert abs(datetime.fromisoformat(event["time"]).timestamp() - before) < 2
            assert side.pending() == []

    run(main)


def test_events_wait_while_the_robot_is_offline() -> None:
    async def main() -> None:
        async with robot_and_cloud(waiting_events=2) as (link, cloud):
            for on in (True, False, True):  # the first one is dropped: only two wait
                await link.emit("lamp.changed", {"on": on})
            side, _ = await connect(link, cloud, events=("lamp.changed",))
            events = [await side.next("event"), await side.next("event")]
            assert [event["data"] for event in events] == [{"on": False}, {"on": True}]

    run(main)


def test_the_owners_choices_reach_the_cloud_as_they_change() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            side, _ = await connect(link, cloud)
            await link.describe(DEVICE)  # nothing changed
            allowed = {SafetyClass.INFORMATIONAL, SafetyClass.PRIVACY}
            await link.describe(
                DeviceInfo("robot", "concept-v0", ("en", "vi"), "Asia/Ho_Chi_Minh", allowed)
            )
            changed = await side.next("device.changed")
            assert changed["languages"] == ["en", "vi"]
            assert changed["allowed_classes"] == ["informational", "privacy"]
            assert "time_zone" not in changed
            assert side.pending() == []

    run(main)


def test_what_changes_during_the_hello_follows_it() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            await link.describe(DEVICE)
            await link.offer([LAMP], [])
            side = await cloud.connection()
            await side.next("hello")
            await link.describe(DeviceInfo("robot", "concept-v0", ("vi",), "UTC", set()))
            await side.hello()
            changed = await side.next("device.changed")
            assert changed["time_zone"] == "UTC" and changed["allowed_classes"] == []

    run(main)


# ---- staying connected ----


def test_pings_say_what_was_received_and_pongs_free_what_was_sent() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            side, _ = await connect(link, cloud, events=("lamp.changed",), ping_interval=0.05)
            await side.mcp("server/discover")
            await side.next("mcp")
            await link.emit("lamp.changed", {"on": True})
            await side.next("event")
            await until(lambda: len(side.pings) >= 2)
            assert side.pings[-1] == {"type": "ping", "received": 1}
            await until(lambda: link.session is not None and not link.session.kept)

    run(main)


def test_a_late_pong_makes_the_robot_connect_again_and_resume() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            side, _ = await connect(link, cloud, ping_interval=0.05)
            await side.mcp("server/discover")
            await side.next("mcp")
            side.answer_pings = False
            await asyncio.wait_for(side.closed.wait(), TIMEOUT)
            side = await cloud.connection()
            hello = await side.next("hello")
            assert hello["resume"] == {"session": SESSION, "received": 1}

    run(main)


def test_a_resumed_session_sends_again_what_the_cloud_did_not_receive() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            side, _ = await connect(link, cloud, events=("lamp.changed",))
            await side.mcp("server/discover")
            for on in (True, False):
                await link.emit("lamp.changed", {"on": on})
            sent = [await side.next() for _ in range(3)]
            assert [message["seq"] for message in sent] == [1, 2, 3]
            side, hello = await reconnection(cloud, side)
            assert hello["resume"] == {"session": SESSION, "received": 1}
            await side.hello(SESSION, resumed=True, received=1, sent=1, events=("lamp.changed",))
            assert [await side.next() for _ in range(2)] == sent[1:]  # same numbers
            await link.emit("lamp.changed", {"on": True})
            assert (await side.next("event"))["seq"] == 4
            side.sent = 0
            await side.mcp("server/discover", request="again")  # number 1, taken already
            await side.mcp("server/discover", request=2)
            answer = await side.next("mcp")
            assert answer["reply_to"] == 2 and answer["payload"]["id"] == 2

    run(main)


def test_a_call_in_progress_survives_a_resumed_session() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            side, _ = await connect(link, cloud)
            asked = await side.mcp("tools/call", {"name": "lamp.set", "arguments": {"on": True}})
            call = await request(link)
            side, _ = await reconnection(cloud, side)
            await side.hello(SESSION, resumed=True, received=0, sent=1)
            assert isinstance(call, ToolCall)
            await link.reply(call, data={"on": True})
            assert (await side.next("mcp"))["reply_to"] == asked["seq"]

    run(main)


def test_a_new_session_cancels_the_calls_in_progress() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            side, _ = await connect(link, cloud)
            await side.mcp("tools/call", {"name": "lamp.set", "arguments": {"on": True}})
            call = await request(link)
            side, _ = await reconnection(cloud, side)
            await side.hello(OTHER_SESSION)  # the cloud lost the session
            assert await request(link) == CallCancelled(call.id)
            await status_where(link, online=True, session=OTHER_SESSION)
            await side.mcp("server/discover")
            assert (await side.next("mcp"))["seq"] == 1  # numbers start again

    run(main)


def test_a_session_that_lost_too_much_cannot_resume() -> None:
    async def main() -> None:
        async with robot_and_cloud(replay=2) as (link, cloud):
            side, _ = await connect(link, cloud, events=("lamp.changed",))
            for on in (True, False, True):
                await link.emit("lamp.changed", {"on": on})
            for _ in range(3):
                await side.next("event")
            side, _ = await reconnection(cloud, side)
            await side.hello(SESSION, resumed=True, received=0)  # it kept only two
            assert await side.next("bye") == {"type": "bye", "reason": "resume_failed"}
            side = await cloud.connection()
            assert "resume" not in await side.next("hello")

    run(main)


def test_the_cloud_may_ask_the_robot_to_connect_again() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            side, _ = await connect(link, cloud)
            await side.send({"type": "reconnect"})
            side = await cloud.connection()
            assert (await side.next("hello"))["resume"]["session"] == SESSION

    run(main)


def test_a_message_that_skips_a_number_ends_the_session() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            side, _ = await connect(link, cloud)
            await side.mcp("server/discover")
            await side.next("mcp")
            side.sent += 1  # as if a message had been lost
            await side.mcp("server/discover")
            assert await side.next("bye") == {"type": "bye", "reason": "out_of_sequence"}
            side = await cloud.connection()
            assert "resume" not in await side.next("hello")

    run(main)


# ---- ends and refusals ----


@pytest.mark.parametrize("answer_with", ["bye", "refusal"])
def test_the_robot_learns_that_it_needs_an_update(
    answer_with: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(link_module.random, "uniform", lambda low, high: high)

    async def main() -> None:
        async with robot_and_cloud(reconnect_first=0.001, reconnect_max=1.0) as (link, cloud):
            await link.describe(DEVICE)
            await link.offer([], [])
            if answer_with == "refusal":
                cloud.refuse = 410
            else:
                side = await cloud.connection()
                await side.next("hello")
                await side.send({"type": "bye", "reason": "update_required"})
            await status_where(link, update_required=True, online=False)
            attempts = len(cloud.assertions)
            await asyncio.sleep(0.5)
            assert len(cloud.assertions) == attempts  # it tries again only after reconnect_max

    run(main)


def test_a_revoked_robot_is_not_set_up_any_more() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            side, _ = await connect(link, cloud)
            cloud.robots.clear()
            await side.send({"type": "bye", "reason": "revoked"})
            await status_where(link, online=False, bound=False)
            assert link.session is None

    run(main)


def test_a_refused_token_brings_a_new_one() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            cloud.refuse_tokens = 1
            await link.describe(DEVICE)
            await link.offer([], [])
            side = await cloud.connection()
            await side.next("hello")
            assert cloud.bearers == ["Bearer access-1", "Bearer access-2"]  # 401, then a new one

    run(main)


def test_the_robot_waits_as_long_as_a_refusal_asks() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            cloud.refuse = 503
            cloud.refuse_headers = {"Retry-After": "1"}
            await link.describe(DEVICE)
            await link.offer([], [])
            started = time.monotonic()
            await until(lambda: cloud.refusals == 1)
            cloud.refuse = None
            side = await cloud.connection()
            assert time.monotonic() - started >= 1.0
            await side.next("hello")

    run(main)


def test_the_robot_waits_as_long_as_a_token_refusal_asks() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            cloud.refuse_token = (429, "temporarily_unavailable", {"Retry-After": "1"})
            started = time.monotonic()
            await link.describe(DEVICE)
            await link.offer([], [])
            await until(lambda: cloud.token_refusals == 1)
            cloud.refuse_token = None
            side = await cloud.connection()
            assert time.monotonic() - started >= 0.5  # it asked for 1 s: a wide margin
            assert cloud.token_refusals == 1
            await side.next("hello")
            await side.hello()
            await status_where(link, online=True, bound=True)  # still set up

    run(main)


def test_the_robot_does_not_follow_a_redirect() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            cloud.redirect_link = cloud.url + "/elsewhere"
            await link.describe(DEVICE)
            await link.offer([], [])
            await until(lambda: cloud.refusals >= 2)  # it tries again, as after a refusal
            assert cloud.elsewhere == []  # its token went nowhere else

    run(main)


def test_retry_after_may_be_a_date() -> None:
    later = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + 30))
    assert 25 < (retry_after({"Retry-After": later}) or 0) <= 30
    earlier = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() - 30))
    assert retry_after({"Retry-After": earlier}) == 0
    assert retry_after({"Retry-After": "soon"}) is None
    assert retry_after({"Retry-After": "7"}) == 7
    assert retry_after({"Retry-After": "999999999"}) == 86400
    assert retry_after({}) is None and retry_after(None) is None


@pytest.mark.parametrize(
    ("value", "wait"),
    [
        (" 12\t", 12),  # whitespace around a value is not in it
        ("9" * 5000, 86400),  # more digits than int() takes
        ("Mon, 31 Dec 9999 23:59:59 -2359", 86400),  # past datetime's last year in UTC
        # delay-seconds is digits only (RFC 9110, §10.2.3), which int() and float() are not.
        ("-5", None),
        ("1_000", None),
        ("1e3", None),
        ("+5", None),
        ("١٢", None),  # Arabic-Indic digits
        ("²", None),  # a superscript two
        # Dates that datetime cannot hold.
        ("Mon, 01 Jan 99999999999999999999 00:00:00 GMT", None),
        ("Mon, 01 Jan 10000 00:00:00 GMT", None),
        ("Mon, 01 Jan 0001 00:00:00 +2359", 0),
        ("Mon, 32 Jan 2026 00:00:00 GMT", None),
        ("Mon, 01 Jan 2026 00:00:00 +9999", None),
        ("", None),
        (" ", None),
    ],
)
def test_retry_after_reads_what_rfc_9110_allows(value: str, wait: float | None) -> None:
    assert retry_after({"Retry-After": value}) == wait


def test_a_cloud_that_does_not_answer_the_hello_gets_a_bye() -> None:
    async def main() -> None:
        async with robot_and_cloud(hello_timeout=0.2) as (link, cloud):
            await link.describe(DEVICE)
            await link.offer([], [])
            side = await cloud.connection()
            await side.next("hello")
            assert await side.next("bye") == {"type": "bye", "reason": "no_hello"}

    run(main)


def test_a_cloud_speaking_another_major_version_gets_a_bye() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            await link.describe(DEVICE)
            await link.offer([], [])
            side = await cloud.connection()
            await side.next("hello")
            await side.hello(protocol="4.0")
            assert await side.next("bye") == {"type": "bye", "reason": "unsupported_protocol"}
            # A later version may change the hello: its version is read before the rest.
            side = await cloud.connection()
            await side.next("hello")
            await side.send({"type": "hello", "protocol": "4.0"})
            assert await side.next("bye") == {"type": "bye", "reason": "unsupported_protocol"}

    run(main)


# ---- what the cloud sends wrong ----


def test_invalid_messages_get_an_error_and_the_session_goes_on() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            side, _ = await connect(link, cloud)
            bad = await side.send({"type": "mcp", "payload": {"jsonrpc": "1.0"}})
            error = await side.next("error")
            assert (error["reply_to"], error["code"]) == (bad["seq"], "invalid_message")
            odd = await side.send({"type": "No Such Type"})  # a string, with its number
            error = await side.next("error")
            assert (error["reply_to"], error["code"]) == (odd["seq"], "invalid_message")
            asked = await side.mcp("server/discover")
            assert (await side.next("mcp"))["reply_to"] == asked["seq"]
            # A number that JSON Schema reads as an integer is a number.
            side.sent += 1
            await side.ws.send_str(json.dumps({**asked, "seq": float(side.sent)}))
            assert (await side.next("mcp"))["reply_to"] == side.sent

    run(main)


SECRET = "secret"  # in frames that the robot cannot answer: its bye must not say it
CANNOT_ANSWER = [
    pytest.param(b"\x00secret", id="binary"),
    pytest.param("not JSON, secret", id="not JSON"),
    pytest.param('{"type": "mcp", "seq": 1, "secret": NaN}', id="NaN"),
    pytest.param('["mcp", "secret"]', id="not an object"),
    pytest.param('{"seq": 1, "secret": 1}', id="no type"),
    pytest.param('{"type": 7, "seq": 1}', id="a type that is no string"),
    pytest.param('{"type": "mcp", "payload": {"secret": 1}}', id="no number"),
    *(
        pytest.param(json.dumps({"type": "mcp", "seq": seq}), id=f"seq {seq!r}")
        for seq in (0, -1, 1.5, "1", True, None, 2**53)
    ),
    pytest.param('{"type": "pong", "secret": 1}', id="a pong that fails"),
    pytest.param('{"type": "reconnect", "message": 5}', id="a reconnect that fails"),
    pytest.param('{"type": "hello", "secret": 1}', id="a hello that fails"),
    pytest.param('{"type": "ping", "x": "secret\\u0000"}', id="U+0000"),
    pytest.param('{"type": "ping", "secret\\ud800": 1}', id="a lone surrogate"),
    pytest.param('{"type": "ping", "x": ' + "[" * 64 + '"secret"' + "]" * 64 + "}", id="65 deep"),
    pytest.param('{"type": "pong", "received": 0, "received": 0}', id="a repeated name"),
]


@pytest.mark.parametrize("frame", CANNOT_ANSWER)
@pytest.mark.parametrize("first", [False, True], ids=["in a session", "before the hello"])
def test_a_frame_the_robot_cannot_answer_ends_the_session(frame: str | bytes, first: bool) -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            side, _ = await connect(link, cloud)
            if first:
                side, _ = await reconnection(cloud, side)
            if isinstance(frame, bytes):
                await side.ws.send_bytes(frame)
            else:
                await side.ws.send_str(frame)
            bye = await side.next("bye")
            assert bye["reason"] == "invalid_message"
            assert bye["message"] and SECRET not in bye["message"]  # why, naming no value
            side = await cloud.connection()
            assert "resume" not in await side.next("hello")  # a new session

    run(main)


FORGED = "x\n2026-11-19 01:00:00 CRITICAL a line the cloud wrote"  # in a type


@pytest.mark.parametrize(
    ("first", "logged"),
    [
        (
            {"type": "mcp", "seq": 1, "payload": {"jsonrpc": "2.0", "method": "x"}},
            "a mcp message came before the cloud's hello",
        ),
        ({"type": "ping"}, "a ping message came before the cloud's hello"),
        ({"type": "pong", "received": 0}, "a pong message came before the cloud's hello"),
        # A type the schema refuses is the cloud's text, which the log must not hold.
        ({"type": FORGED, "seq": 1}, "a message came before the cloud's hello: type: breaks"),
    ],
    ids=["a numbered message", "a ping", "a pong", "a type that is not valid"],
)
def test_a_first_message_that_is_no_hello_gets_a_bye(
    first: dict[str, Any], logged: str, caplog: pytest.LogCaptureFixture
) -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            side, _ = await connect(link, cloud)
            side, said = await reconnection(cloud, side)
            assert said["resume"]["session"] == SESSION
            await side.ws.send_str(json.dumps(first))
            assert await side.next("bye") == {"type": "bye", "reason": "no_hello"}
            side = await cloud.connection()
            assert "resume" not in await side.next("hello")  # the bye ended the session

    run(main)
    assert logged in caplog.text and "a line the cloud wrote" not in caplog.text


def test_a_hello_that_resumes_a_session_the_robot_did_not_name_gets_a_bye() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            side, _ = await connect(link, cloud)
            side, said = await reconnection(cloud, side)
            assert said["resume"]["session"] == SESSION
            await side.hello(OTHER_SESSION, resumed=True, received=0)
            assert await side.next("bye") == {"type": "bye", "reason": "no_hello"}
            side = await cloud.connection()
            assert "resume" not in await side.next("hello")  # the bye ended the session

    run(main)


@pytest.mark.parametrize("first", [False, True], ids=["in a session", "before the hello"])
def test_a_bye_from_the_cloud_ends_the_session_whatever_it_holds(first: bool) -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            side, _ = await connect(link, cloud)
            if first:
                side, _ = await reconnection(cloud, side)
            await side.send({"type": "bye", "reason": "Not A Code"})
            await asyncio.wait_for(side.closed.wait(), TIMEOUT)
            assert side.pending() == []  # no bye answers a bye
            side = await cloud.connection()
            assert "resume" not in await side.next("hello")

    run(main)


def notification(seq: int, size: int) -> str:
    """An MCP notification numbered ``seq``, of ``size`` bytes."""

    def text(padding: str) -> str:
        payload = {"jsonrpc": "2.0", "method": "notifications/x", "params": {"x": padding}}
        return json.dumps({"type": "mcp", "seq": seq, "payload": payload})

    return text("x" * (size - len(text(""))))


def test_a_message_as_large_as_the_protocol_allows_is_taken() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            side, _ = await connect(link, cloud)
            side.sent += 1
            await side.ws.send_str(notification(side.sent, MAX_MESSAGE))
            asked = await side.mcp("server/discover")
            assert (await side.next("mcp"))["reply_to"] == asked["seq"]

    run(main)


def test_a_message_too_large_ends_the_session() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            side, _ = await connect(link, cloud)
            side.sent += 1
            await side.ws.send_str(notification(side.sent, MAX_MESSAGE + 1))
            await asyncio.wait_for(side.closed.wait(), TIMEOUT)
            assert side.close_code == 1009  # "message too big": no bye can follow
            side = await cloud.connection()
            assert "resume" not in await side.next("hello")

    run(main)


def test_the_cloud_s_error_reaches_the_log_quoted(caplog: pytest.LogCaptureFixture) -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            side, _ = await connect(link, cloud)
            error = {"type": "error", "reply_to": 1, "code": "internal", "message": FORGED}
            await side.send(error)
            asked = await side.mcp("server/discover")
            assert (await side.next("mcp"))["reply_to"] == asked["seq"]

    run(main)
    assert f"the cloud reports internal about message 1: {FORGED!r}" in caplog.messages


def test_the_kinds_of_message_ignored_are_logged_once_and_at_most_max_warned(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            side, _ = await connect(link, cloud)
            for kind in [f"other.k{number}" for number in range(MAX_WARNED + 1)] * 2:
                await side.send({"type": kind})
            asked = await side.mcp("server/discover")  # after them all: the session goes on
            assert (await side.next("mcp"))["reply_to"] == asked["seq"]

    with caplog.at_level(logging.INFO, logger="ai_robot.adapters.websocket.session"):
        run(main)
    ignored = [line for line in caplog.messages if line.startswith("ignoring other.k")]
    assert ignored == [
        f"ignoring other.k{number} messages from the cloud" for number in range(MAX_WARNED)
    ]


def test_a_connection_the_cloud_closes_with_1009_ends_the_session(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            side, _ = await connect(link, cloud)
            await side.ws.close(code=1009)  # the robot's message was too large for it
            side = await cloud.connection()
            assert "resume" not in await side.next("hello")
            assert "the cloud ended the session: invalid_message" in caplog.text  # not the robot
            await side.hello()
            await side.ws.close(code=1000)  # any other close: the session resumes
            side = await cloud.connection()
            assert (await side.next("hello"))["resume"]["session"] == SESSION

    run(main)


def test_text_that_is_not_utf_8_ends_the_session() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            side, _ = await connect(link, cloud)
            # send_str() encodes what it sends: the writer sends these bytes as they are.
            writer = side.ws._writer
            assert writer is not None
            await writer.send_frame(b'{"type": "\xff"}', WSMsgType.TEXT)
            await asyncio.wait_for(side.closed.wait(), TIMEOUT)
            assert side.close_code == 1007  # aiohttp's, before any bye
            side = await cloud.connection()
            assert "resume" not in await side.next("hello")

    run(main)


def test_what_the_cloud_cannot_hold_is_not_sent() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            side, _ = await connect(link, cloud, capabilities=(FAN,), events=("lamp.changed",))
            for data in ({"x": "\x00"}, {"x": "\ud800"}, {"x": "x" * (1 << 20)}):
                await link.emit("lamp.changed", data)
                call_fan = {"name": "fan.set", "arguments": {"on": True}}
                asked = await side.mcp("tools/call", call_fan)
                call = await request(link)
                assert isinstance(call, ToolCall)
                await link.reply(call, data=data)
                answer = await side.next("mcp")  # no event came before it
                assert answer["reply_to"] == asked["seq"]
                result = answer["payload"]["result"]
                assert result["isError"] is True
                assert result["content"] == [{"type": "text", "text": mcp_module.UNSENDABLE}]

    run(main)


def test_a_bye_s_message_fits_the_protocol() -> None:
    class Socket:
        closed = False
        sent: ClassVar[list[str]] = []

        async def send_str(self, text: str) -> None:
            self.sent.append(text)

        async def close(self) -> None:
            self.closed = True

    async def main() -> None:
        socket = Socket()
        connection = Connection(cast(Any, socket), schema("from_device"), TIMEOUT)
        ending = Ending(greeted=True, reason="invalid_message", message="ồ" * (MAX_BYE + 1))
        assert await connection.end(ending) is ending
        (said,) = socket.sent
        bye = json.loads(said)
        assert bye["message"] == "ồ" * MAX_BYE  # characters, not bytes
        assert schema("from_device").problem(bye) is None and socket.closed

    run(main)


def test_too_many_messages_are_dropped() -> None:
    async def main() -> None:
        async with robot_and_cloud(rate=1.0, burst=1, backlog=1) as (link, cloud):
            side, _ = await connect(link, cloud)
            for request_id in range(5):
                await side.mcp("server/discover", request=request_id)
            while (message := await side.next())["type"] != "error":
                assert message["type"] == "mcp"  # the answers to those it took
            assert message["code"] == "rate_limited"

    run(main)


def test_a_robot_that_offers_no_conversations_holds_none() -> None:
    async def main() -> None:
        async with robot_and_cloud() as (link, cloud):
            await link.describe(DEVICE)
            await link.offer([LAMP], [])
            side = await cloud.connection()
            said = await side.next("hello")
            assert (said["conversations"], said["media"]) == (False, [])
            await side.hello(conversations=True)
            assert (await status_where(link, online=True)).conversations is False
            start = {
                "type": "conversation.start",
                "conversation": "01a18086-9160-765a-a90f-9dd0604d45f2",
                "listen": False,
                "media": {"livekit": {"url": "wss://media.example", "token": "t"}},
            }
            sent = await side.send(start)
            error = await side.next("error")
            assert (error["reply_to"], error["code"]) == (sent["seq"], "not_allowed")
            end = {"type": "conversation.end", "conversation": start["conversation"]}
            await side.send({**end, "reason": "done"})
            assert (await side.next("error"))["code"] == "unknown_conversation"
            await link.converse(ConversationRequest("c1", Trigger.WAKE_WORD))
            assert await asyncio.wait_for(link.conversation(), TIMEOUT) == ConversationEnd(
                "c1", "unavailable"
            )
            await link.listening(False)
            assert link.may_listen is False

    run(main)
