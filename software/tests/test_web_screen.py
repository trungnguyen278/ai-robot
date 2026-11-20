# SPDX-License-Identifier: MIT
"""The web screen: serves the renderer and speaks the screen protocol with its pages."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from ai_robot.adapters.web.screen import (
    PROTOCOL,
    REFUSED_HOST,
    WebScreen,
    own_names,
    panel_message,
)
from ai_robot.ports import (
    Action,
    Close,
    Hold,
    Opened,
    Panel,
    PanelItem,
    PanelSection,
    ScreenState,
    Touch,
    Woken,
)

VOCABULARY = Path(__file__).resolve().parents[2] / "protocol" / "screen" / "vocabulary.json"
TIMEOUT = 2.0
SETTINGS: dict[str, Any] = {  # as shipped
    "send_timeout": 1.0,
    "heartbeat": 10.0,
    "close_timeout": 2.0,
    "allowed_hosts": [],
}


@pytest.fixture
def ui_dir(tmp_path: Path) -> Path:
    (tmp_path / "screen.html").write_text("<!doctype html><title>Screen</title>", "utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.js").write_text("export {};", "utf-8")
    return tmp_path


@contextlib.asynccontextmanager
async def serving(ui_dir: Path, **settings: Any) -> AsyncIterator[tuple[WebScreen, str]]:
    """An open WebScreen on a free port, and its address."""
    settings = SETTINGS | {"host": "127.0.0.1", "port": 0} | settings
    screen = WebScreen(**settings, ui_dir=str(ui_dir))
    await screen.open()
    try:
        assert screen.address is not None
        yield screen, "http://{}:{}".format(*screen.address)
    finally:
        await screen.close()


async def receive(socket: aiohttp.ClientWebSocketResponse) -> Any:
    return await socket.receive_json(timeout=TIMEOUT)


async def receive_state(socket: aiohttp.ClientWebSocketResponse) -> dict[str, Any]:
    """The next message, a state, without the software's clock reading."""
    message = await receive(socket)
    assert message["type"] == "state"
    assert abs(message.pop("time") - time.time()) < 5
    return message


def ready(**lists: list[str]) -> dict[str, Any]:
    return {
        "type": "ready",
        "protocol": PROTOCOL,
        "width": 1024,
        "height": 600,
        "shape": "rect",
    } | lists


def test_protocol_version_matches_the_vocabulary() -> None:
    assert json.loads(VOCABULARY.read_text(encoding="utf-8"))["protocol"] == PROTOCOL


def test_serves_this_checkouts_renderer_by_default() -> None:
    screen = WebScreen(**SETTINGS, host="127.0.0.1", port=0)
    assert (screen.ui_dir / "screen.html").is_file()


def test_settings_are_checked(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"no screen\.html"):
        WebScreen(**SETTINGS, host="127.0.0.1", port=0, ui_dir=str(tmp_path))
    with pytest.raises(ValueError, match="port"):
        WebScreen(**SETTINGS, host="127.0.0.1", port=70000)


@pytest.mark.parametrize(
    ("origin", "host", "connects"),
    [
        (None, None, True),  # no browser's
        ("{base}", None, True),  # the screen's own page
        ("http://ROBOT.local:{port}", "robot.LOCAL:{port}", True),
        ("https://attacker.example", None, False),
        ("http://127.0.0.1:1", None, False),  # another server on this computer
        ("null", None, False),  # a page with no origin a browser names, such as a file
    ],
)
def test_only_the_screens_own_page_drives_it(
    ui_dir: Path, origin: str | None, host: str | None, connects: bool
) -> None:
    """A page from another site would drive the robot, as the owner's finger does."""

    async def main() -> None:
        async with (
            serving(ui_dir, allowed_hosts=["robot.local"]) as (screen, base),
            aiohttp.ClientSession() as session,
        ):
            port = base.rsplit(":", 1)[1]
            headers = {}
            if origin is not None:
                headers["Origin"] = origin.format(base=base, port=port)
            if host is not None:
                headers["Host"] = host.format(port=port)
            if connects:
                async with session.ws_connect(f"{base}/ws", headers=headers) as page:
                    assert (await receive(page))["type"] == "hello"
                return
            with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
                await session.ws_connect(f"{base}/ws", headers=headers)
            assert refused.value.status == 403
            assert not screen.pages

    asyncio.run(main())


@pytest.mark.parametrize(
    ("host", "answers"),
    [
        ("127.0.0.1:{port}", True),  # an address: no name is resolved for it
        ("192.0.2.7", True),
        ("[::1]:{port}", True),
        ("localhost:{port}", True),  # always this computer (RFC 6761, 6.3)
        ("screen.localhost", True),
        ("robot-7.local:{port}", True),  # this computer's mDNS name, from its host name
        ("ROBOT-7.LOCAL.:{port}", True),
        ("listening.example", True),  # where it listens
        ("robot.example.lan", True),  # listed
        ("example.org", True),  # listed with a dot, as are the names under it
        ("a.b.example.org", True),
        ("attacker.example:{port}", False),  # a name rebound to the robot's address
        ("robot-7", False),
        ("robot-7.example.local", False),
        ("notexample.org", False),
        ("example.org.attacker.example", False),
        ("127.0.0.1.attacker.example", False),
        ("", False),
        ("[robot]", False),
        ("[dead:beef]", False),  # no IPv6 address
        ("robot.example.lan:80:80", False),
        ("robot.example.lan/x", False),
    ],
)
def test_answers_only_to_its_own_host_names(
    ui_dir: Path, monkeypatch: pytest.MonkeyPatch, host: str, answers: bool
) -> None:
    """A page of another site, whose name its owner points at the robot's address (DNS
    rebinding), would get the screen as its own, and drive the robot through it."""
    monkeypatch.setattr("socket.gethostname", lambda: "Robot-7.example.lan")
    monkeypatch.setattr(
        "ai_robot.adapters.web.screen.own_names",
        lambda listening: own_names("listening.example"),
    )

    async def main() -> None:
        listed = ["Robot.Example.LAN.", ".example.org"]
        async with (
            serving(ui_dir, allowed_hosts=listed) as (screen, base),
            aiohttp.ClientSession() as session,
        ):
            headers = {"Host": host.format(port=base.rsplit(":", 1)[1])}
            for path in ("/", "/src/app.js", "/nothing.js"):
                async with session.get(f"{base}{path}", headers=headers) as response:
                    if answers:
                        assert response.status == (404 if path == "/nothing.js" else 200)
                    else:
                        assert response.status == 403
                        assert await response.text() == REFUSED_HOST
            if answers:
                async with session.ws_connect(f"{base}/ws", headers=headers) as page:
                    assert (await receive(page))["type"] == "hello"
                return
            with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
                await session.ws_connect(f"{base}/ws", headers=headers)
            assert refused.value.status == 403
            assert not screen.pages

    asyncio.run(main())


async def ask(base: str, request: str) -> bytes:
    """The status code of the answer to ``request``, sent as it is written, with
    ``{port}`` replaced by the screen's."""
    host, port = base.removeprefix("http://").split(":")
    reader, writer = await asyncio.open_connection(host, int(port))
    try:
        writer.write(request.format(port=port).encode())
        return (await asyncio.wait_for(reader.readline(), TIMEOUT)).split()[1]
    finally:
        writer.close()


def test_a_request_without_a_host_is_no_browsers(ui_dir: Path) -> None:
    """Every browser names the host; a client that names none is taken to ask for the
    address it reached (RFC 9112, 3.3)."""

    async def main() -> None:
        async with serving(ui_dir) as (_, base):
            assert await ask(base, "GET / HTTP/1.0\r\n\r\n") == b"200"
            twice = "GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nHost: attacker.example\r\n\r\n"
            assert await ask(base, twice) == b"400"

    asyncio.run(main())


UPGRADE = (
    "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
    "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
)


@pytest.mark.parametrize(
    ("request_", "status"),
    [
        # The target names another site, Host the robot: both checks read the target's.
        ("GET http://attacker.example/ HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n", b"403"),
        (
            "GET http://attacker.example/ws HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            f"Origin: http://attacker.example\r\n{UPGRADE}\r\n",
            b"403",
        ),
        # The target names the robot: Host is not read.
        ("GET http://127.0.0.1:{port}/ HTTP/1.1\r\nHost: attacker.example\r\n\r\n", b"200"),
        (
            "GET http://127.0.0.1:{port}/ws HTTP/1.1\r\nHost: attacker.example\r\n"
            f"Origin: http://attacker.example\r\n{UPGRADE}\r\n",
            b"403",
        ),
        (
            f"GET http://127.0.0.1:{{port}}/ws HTTP/1.1\r\nHost: attacker.example\r\n{UPGRADE}\r\n",
            b"101",
        ),
    ],
)
def test_a_whole_url_as_the_target_names_the_host(
    ui_dir: Path, request_: str, status: bytes
) -> None:
    """A client may name the host in the request's target, as it does to a proxy. The
    target's host is then the one the request is for, whatever Host says (RFC 9112,
    3.2.2), for the host check and the origin check alike. No browser sends such a request
    to the robot, but one check must not read one host and the other another."""

    async def main() -> None:
        async with serving(ui_dir) as (_, base):
            assert await ask(base, request_) == status

    asyncio.run(main())


def test_its_own_names_come_from_the_system(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("socket.gethostname", lambda: "Robot-7.example.lan")
    assert own_names("0.0.0.0") == ["Robot-7.local"]  # Avahi's name: up to the first dot
    assert own_names("192.168.1.47") == ["Robot-7.local"]
    assert own_names("::") == ["Robot-7.local"]
    assert own_names("") == ["Robot-7.local"]
    assert own_names("robot.example.lan") == ["Robot-7.local", "robot.example.lan"]
    # Where only this computer reaches the screen, it has no name on the network.
    assert own_names("127.0.0.1") == []
    assert own_names("127.0.0.2") == []
    assert own_names("::1") == []
    assert own_names("LOCALHOST.") == ["LOCALHOST."]
    assert own_names("screen.localhost") == ["screen.localhost"]
    monkeypatch.setattr("socket.gethostname", lambda: "")
    assert own_names("0.0.0.0") == []


def test_a_screen_only_this_computer_reaches_has_no_network_name(
    ui_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("socket.gethostname", lambda: "robot-7")

    async def main() -> None:
        async with (
            serving(ui_dir) as (_, base),
            aiohttp.ClientSession() as session,
            session.get(base, headers={"Host": "robot-7.local"}) as response,
        ):
            assert response.status == 403

    asyncio.run(main())


@pytest.mark.parametrize(
    "listed",
    [
        "robot",  # a text, not a list
        ["robot.example.lan:8770"],  # a port is no part of a name
        ["http://robot.example.lan"],
        ["*"],
        [""],
        ["."],
        ["robot..example"],
        ["робот.example"],  # as a browser sends it: robot's punycode, xn--...
        [8770],
    ],
)
def test_the_listed_host_names_are_checked(listed: Any) -> None:
    with pytest.raises(ValueError, match="allowed_hosts") as error:
        WebScreen(**SETTINGS | {"allowed_hosts": listed}, host="127.0.0.1", port=0)
    named = ("8770", "http", "*", "..", "робот")  # what the values above hold, not the example
    assert not any(value in str(error.value) for value in named)


def test_refusals_are_logged_once_each(ui_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    async def main() -> None:
        async with serving(ui_dir) as (_, base), aiohttp.ClientSession() as session:
            for _ in range(3):
                async with session.get(base, headers={"Host": "attacker.example"}) as response:
                    assert response.status == 403
                with pytest.raises(aiohttp.WSServerHandshakeError):
                    await session.ws_connect(f"{base}/ws", headers={"Origin": "http://x.example"})

    with caplog.at_level(logging.DEBUG, logger="ai_robot.adapters.web.screen"):
        asyncio.run(main())
    refusals = [record for record in caplog.records if "refused" in record.getMessage()]
    assert [record.levelno for record in refusals] == [logging.WARNING] * 2 + [logging.DEBUG] * 4
    assert not any("attacker" in record.getMessage() for record in caplog.records)


def test_no_page_shows_the_screen_in_a_frame(ui_dir: Path) -> None:
    """Another site would lay the screen under a decoy, and the owner's finger would drive
    the robot unawares (clickjacking): a press and hold opens the settings, and two taps
    reset the robot."""

    async def main() -> None:
        async with serving(ui_dir) as (_, base), aiohttp.ClientSession() as session:
            asked = [("/", {}), ("/src/app.js", {}), ("/", {"Host": "attacker.example"})]
            for path, headers in asked:
                async with session.get(f"{base}{path}", headers=headers) as response:
                    assert response.headers["Content-Security-Policy"] == "frame-ancestors 'none'"
                    assert response.headers["X-Frame-Options"] == "DENY"
                    assert response.headers["Cache-Control"] == "no-cache"

    asyncio.run(main())


def test_serves_the_renderer_and_drives_its_pages(ui_dir: Path) -> None:
    async def main() -> None:
        async with serving(ui_dir) as (screen, base), aiohttp.ClientSession() as session:
            async with session.get(f"{base}/") as response:
                assert response.status == 200
                assert "<title>Screen" in await response.text()
                assert response.headers["Cache-Control"] == "no-cache"
            async with session.get(f"{base}/src/app.js") as response:
                assert response.status == 200
            async with session.get(f"{base}/src/../../secret.txt") as response:
                assert response.status in (403, 404)

            await screen.show(
                ScreenState(expression="happy", battery=50.0, charging=False, battery_low=False)
            )
            async with session.ws_connect(f"{base}/ws") as page:
                # A page that connects gets the whole state at once.
                hello = await receive(page)
                assert hello["type"] == "hello" and hello["protocol"] == PROTOCOL
                assert hello["revision"] == screen.revision and len(screen.revision) == 16
                assert abs(hello["time"] - time.time()) < 5
                assert await receive_state(page) == {
                    "type": "state",
                    "face": {"expression": "happy", "tone": None},
                    "scene": None,
                    "status": {"battery": 50.0, "charging": False, "low": False, "muted": None},
                    "panel": None,
                    "asleep": False,
                }
                assert await receive(page) == {"type": "look", "target": None}

                await page.send_json(ready(expressions=["neutral", "happy"], scenes=["clock"]))
                assert await asyncio.wait_for(screen.event(), TIMEOUT) == Opened()
                await page.send_json({"type": "touch", "x": 0.25, "y": 0.75})
                assert await asyncio.wait_for(screen.event(), TIMEOUT) == Touch(0.25, 0.75)
                await page.send_json({"type": "wake"})
                assert await asyncio.wait_for(screen.event(), TIMEOUT) == Woken()

                await screen.look((0.5, -0.25))
                assert await receive(page) == {"type": "look", "target": {"x": 0.5, "y": -0.25}}
                await screen.voice(0.4)
                assert await receive(page) == {"type": "voice", "level": 0.4}
                await screen.show(ScreenState(expression="sad", tone="blue"))
                assert (await receive_state(page))["face"] == {"expression": "sad", "tone": "blue"}
                data = {"condition": "rain", "days": [18, {"high": 19}]}
                await screen.show(ScreenState(scene="weather", scene_data=data))
                assert (await receive_state(page))["scene"] == {"name": "weather", "data": data}
                await screen.show(ScreenState(asleep=True))
                assert (await receive_state(page))["asleep"] is True

    asyncio.run(main())


def test_a_page_that_connects_early_waits_for_the_first_state(ui_dir: Path) -> None:
    async def main() -> None:
        async with (
            serving(ui_dir) as (screen, base),
            aiohttp.ClientSession() as session,
            session.ws_connect(f"{base}/ws") as page,
        ):
            assert (await receive(page))["type"] == "hello"
            with pytest.raises(asyncio.TimeoutError):
                await page.receive_json(timeout=0.1)  # nothing before the first show()
            await screen.show(ScreenState(expression="happy"))
            assert (await receive_state(page))["face"]["expression"] == "happy"

    asyncio.run(main())


def test_warns_about_names_no_page_knows(ui_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    async def main() -> None:
        async with serving(ui_dir) as (screen, base), aiohttp.ClientSession() as session:
            await screen.show(ScreenState())
            async with session.ws_connect(f"{base}/ws") as page:
                for _ in range(3):
                    await receive(page)
                await page.send_json(ready(expressions=["neutral"]))  # an older page: no scenes
                assert await asyncio.wait_for(screen.event(), TIMEOUT) == Opened()  # handled
                for _ in range(2):
                    await screen.show(ScreenState(expression="sad", scene="clock"))
                    await receive(page)

    with caplog.at_level(logging.WARNING, logger="ai_robot.adapters.web.screen"):
        asyncio.run(main())
    warnings = [record.getMessage() for record in caplog.records]
    assert warnings == [
        "no screen page knows the expression 'sad'; it shows its default expression",
        "no screen page knows the scene 'clock'; it shows no scene",
    ]


def test_the_revision_follows_the_renderers_files(ui_dir: Path) -> None:
    async def revision() -> str:
        async with serving(ui_dir) as (screen, _):
            return screen.revision

    first = asyncio.run(revision())
    assert asyncio.run(revision()) == first
    (ui_dir / "src" / "app.js").write_text("export const changed = true;", "utf-8")
    assert asyncio.run(revision()) != first


def test_ignores_what_it_does_not_understand(ui_dir: Path) -> None:
    async def main() -> None:
        async with (
            serving(ui_dir) as (screen, base),
            aiohttp.ClientSession() as session,
            session.ws_connect(f"{base}/ws") as page,
        ):
            await page.send_str("not json")
            await page.send_json(["not", "an", "object"])
            await page.send_json({"type": "from-the-future"})
            await page.send_json({"type": "touch", "x": 2, "y": 0})  # off the screen
            await page.send_json({"type": "touch", "x": True, "y": 0})
            await page.send_json({"type": "touch", "x": 1, "y": 0})
            assert await asyncio.wait_for(screen.event(), TIMEOUT) == Touch(1.0, 0.0)
            await page.send_json({"type": "hold", "x": -1, "y": 0})
            await page.send_json({"type": "action", "panel": "settings"})  # no item
            await page.send_json(
                {"type": "action", "panel": "settings", "item": "a.b", "value": []}
            )
            await page.send_json({"type": "action", "panel": "x" * 65, "item": "a.b"})
            await page.send_json({"type": "close"})
            await page.send_json({"type": "close", "panel": "settings"})
            assert await asyncio.wait_for(screen.event(), TIMEOUT) == Close("settings")

    asyncio.run(main())


PANEL = Panel(
    "settings",
    "Settings",
    (
        PanelSection(
            "Sound",
            (
                PanelItem(
                    "voice.volume", "slider", "Volume", 80, minimum=0, maximum=100, step=5, unit="%"
                ),
                PanelItem(
                    "voice.microphones", "toggle", "Microphones", True, help="Off: nothing heard"
                ),
            ),
        ),
        PanelSection(
            "Wi-Fi and app",
            (
                PanelItem(
                    "setup.start",
                    "button",
                    "Set up with the app",
                    confirm="Open set-up?",
                    confirm_lines=("The app finds the robot over Bluetooth.",),
                    confirm_label="Open",
                ),
                PanelItem("setup.network", "info", "Wi-Fi", "Home, connected"),
                PanelItem(
                    "x.lang",
                    "choice",
                    "Language",
                    "en",
                    choices=(("en", "English"), ("vi", "Tiếng Việt")),
                ),
            ),
        ),
    ),
)


def test_a_panel_is_sent_as_the_protocol_has_it(ui_dir: Path) -> None:
    async def main() -> None:
        async with (
            serving(ui_dir) as (screen, base),
            aiohttp.ClientSession() as session,
            session.ws_connect(f"{base}/ws") as page,
        ):
            await receive(page)  # hello
            await screen.show(ScreenState(panel=PANEL, muted=True))
            state = await receive_state(page)
            assert state["status"]["muted"] is True
            assert state["panel"] == {
                "name": "settings",
                "title": "Settings",
                "sections": [
                    {
                        "title": "Sound",
                        "items": [
                            {
                                "id": "voice.volume",
                                "kind": "slider",
                                "label": "Volume",
                                "value": 80,
                                "min": 0,
                                "max": 100,
                                "step": 5,
                                "unit": "%",
                            },
                            {
                                "id": "voice.microphones",
                                "kind": "toggle",
                                "label": "Microphones",
                                "value": True,
                                "help": "Off: nothing heard",
                            },
                        ],
                    },
                    {
                        "title": "Wi-Fi and app",
                        "items": [
                            {
                                "id": "setup.start",
                                "kind": "button",
                                "label": "Set up with the app",
                                "confirm": "Open set-up?",
                                "confirm_lines": ["The app finds the robot over Bluetooth."],
                                "confirm_label": "Open",
                            },
                            {
                                "id": "setup.network",
                                "kind": "info",
                                "label": "Wi-Fi",
                                "value": "Home, connected",
                            },
                            {
                                "id": "x.lang",
                                "kind": "choice",
                                "label": "Language",
                                "value": "en",
                                "choices": [
                                    {"value": "en", "label": "English"},
                                    {"value": "vi", "label": "Tiếng Việt"},
                                ],
                            },
                        ],
                    },
                ],
            }
            await page.send_json({"type": "hold", "x": 0.5, "y": 0.5})
            assert await asyncio.wait_for(screen.event(), TIMEOUT) == Hold(0.5, 0.5)
            action = {"type": "action", "panel": "settings", "item": "voice.volume", "value": 60}
            await page.send_json(action)
            assert await asyncio.wait_for(screen.event(), TIMEOUT) == Action(
                "settings", "voice.volume", 60
            )
            await page.send_json({"type": "action", "panel": "settings", "item": "setup.start"})
            assert await asyncio.wait_for(screen.event(), TIMEOUT) == Action(
                "settings", "setup.start"
            )
            await page.send_json({"type": "close", "panel": "settings"})
            assert await asyncio.wait_for(screen.event(), TIMEOUT) == Close("settings")

    asyncio.run(main())


def test_no_panel_is_null() -> None:
    assert panel_message(None) is None


@pytest.mark.parametrize(
    ("fields", "problem"),
    [
        ({"kind": "dial"}, "kind is one of"),
        ({"kind": "toggle", "value": "yes"}, "boolean"),
        (
            {"kind": "slider", "value": 120, "minimum": 0, "maximum": 100, "step": 5},
            "minimum <= value",
        ),
        ({"kind": "slider", "value": 50, "minimum": 0, "maximum": 100, "step": 0}, "step > 0"),
        (
            {"kind": "slider", "value": float("nan"), "minimum": 0, "maximum": 1, "step": 0.1},
            "finite",
        ),
        ({"kind": "choice", "value": "fr", "choices": (("en", "English"),)}, "one of its choices"),
        ({"kind": "button", "value": 1}, "no value"),
        ({"kind": "info", "value": 3}, "text"),
        ({"kind": "info", "value": "x", "confirm": "Sure?"}, "confirmation"),
        ({"kind": "button", "confirm_lines": ("Gone.",)}, "only a question"),
        ({"kind": "button", "confirm_label": "Erase"}, "only a question"),
        ({"kind": "button", "confirm": "Sure?", "confirm_lines": ("",)}, "each line"),
        ({"kind": "button", "confirm": "Sure?", "confirm_lines": (3,)}, "each line"),
        ({"kind": "button", "confirm": "Sure?", "confirm_label": ""}, "needs a label"),
    ],
)
def test_items_that_are_not_drawable_are_refused(fields: dict[str, Any], problem: str) -> None:
    with pytest.raises(ValueError, match=problem):
        PanelItem("a.b", label="A", **fields)


def test_items_of_a_panel_have_their_own_ids() -> None:
    item = PanelItem("a.b", "info", "A", "x")
    with pytest.raises(ValueError, match="same id"):
        Panel("settings", "Settings", (PanelSection("About", (item, item)),))


def test_closing_disconnects_the_pages(ui_dir: Path) -> None:
    async def main() -> None:
        screen = WebScreen(**SETTINGS, host="127.0.0.1", port=0, ui_dir=str(ui_dir))
        await screen.open()
        base = "http://{}:{}".format(*screen.address)  # type: ignore[str-format]
        async with aiohttp.ClientSession() as session, session.ws_connect(f"{base}/ws") as page:
            await receive(page)  # hello
            began = time.monotonic()
            closing = asyncio.create_task(screen.close())
            message = await page.receive(timeout=TIMEOUT)
            assert message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
            await closing
            assert time.monotonic() - began < TIMEOUT
            assert screen.pages == {}

    asyncio.run(main())
