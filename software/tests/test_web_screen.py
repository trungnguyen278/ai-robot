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

from ai_robot.adapters.web.screen import PROTOCOL, WebScreen, panel_message
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
)

VOCABULARY = Path(__file__).resolve().parents[2] / "protocol" / "screen" / "vocabulary.json"
TIMEOUT = 2.0
TIMINGS = {"send_timeout": 1.0, "heartbeat": 10.0, "close_timeout": 2.0}  # as shipped


@pytest.fixture
def ui_dir(tmp_path: Path) -> Path:
    (tmp_path / "screen.html").write_text("<!doctype html><title>Screen</title>", "utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.js").write_text("export {};", "utf-8")
    return tmp_path


@contextlib.asynccontextmanager
async def serving(ui_dir: Path) -> AsyncIterator[tuple[WebScreen, str]]:
    """An open WebScreen on a free port, and its address."""
    screen = WebScreen(**TIMINGS, host="127.0.0.1", port=0, ui_dir=str(ui_dir))
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
    screen = WebScreen(**TIMINGS, host="127.0.0.1", port=0)
    assert (screen.ui_dir / "screen.html").is_file()


def test_settings_are_checked(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"no screen\.html"):
        WebScreen(**TIMINGS, host="127.0.0.1", port=0, ui_dir=str(tmp_path))
    with pytest.raises(ValueError, match="port"):
        WebScreen(**TIMINGS, host="127.0.0.1", port=70000)


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
                PanelItem("setup.start", "button", "Set up with the app", confirm="Open set-up?"),
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
        screen = WebScreen(**TIMINGS, host="127.0.0.1", port=0, ui_dir=str(ui_dir))
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
