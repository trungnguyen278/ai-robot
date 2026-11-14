# SPDX-License-Identifier: MIT
"""Screen drawn by the renderer in ui/, in a web browser.

The adapter serves the renderer's files over HTTP and drives every page that connects
over a WebSocket, with the screen protocol (protocol/screen/README.md). On the robot, a
full-screen browser shows the page; on a computer, any browser can.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
from pathlib import Path
from typing import Any

from aiohttp import WSCloseCode, WSMsgType, web

from ai_robot.core.checks import positive
from ai_robot.core.data import encode
from ai_robot.ports.screen import (
    Action,
    Close,
    Hold,
    Opened,
    Panel,
    PanelItem,
    Screen,
    ScreenEvent,
    ScreenState,
    Touch,
)

log = logging.getLogger(__name__)

PROTOCOL = 1
PAGE = "screen.html"
MAX_NAME = 64  # characters in a panel's or an item's name, as a page reports it

# src/ai_robot/adapters/web/screen.py -> the ui/ folder of this checkout
DEFAULT_UI_DIR = Path(__file__).resolve().parents[5] / "ui"


def _fraction(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and 0 <= value <= 1


def _name(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_NAME


def _scalar(value: Any) -> bool:
    """A value a panel's item takes: a boolean, a finite number, a text, or none."""
    if isinstance(value, float):
        return math.isfinite(value)
    return value is None or isinstance(value, bool | int | str)


def panel_message(panel: Panel | None) -> dict[str, Any] | None:
    """``panel`` as the protocol has it (protocol/screen/README.md, Panels)."""
    if panel is None:
        return None
    return {
        "name": panel.name,
        "title": panel.title,
        "sections": [
            {"title": section.title, "items": [_item_message(item) for item in section.items]}
            for section in panel.sections
        ],
    }


def _item_message(item: PanelItem) -> dict[str, Any]:
    message: dict[str, Any] = {"id": item.id, "kind": item.kind, "label": item.label}
    if item.kind != "button":
        message["value"] = item.value
    if item.help:
        message["help"] = item.help
    if item.confirm:
        message["confirm"] = item.confirm
    if item.kind == "slider":
        message.update(min=item.minimum, max=item.maximum, step=item.step)
        if item.unit:
            message["unit"] = item.unit
    if item.kind == "choice":
        message["choices"] = [{"value": value, "label": label} for value, label in item.choices]
    return message


def _revision(folder: Path) -> str:
    """A fingerprint of the renderer's files: it changes whenever one of them does."""
    digest = hashlib.sha256()
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        digest.update(path.relative_to(folder).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


async def _no_cache(request: web.Request, response: web.StreamResponse) -> None:
    """Make the browser check for newer files, so that an update shows on reload."""
    response.headers["Cache-Control"] = "no-cache"


class WebScreen(Screen):
    """Serves the renderer and drives it over a WebSocket.

    The screen is at ``http://<host>:<port>/``. ``ui_dir`` is the renderer's folder;
    by default, the ``ui`` folder of this checkout. A page that takes more than
    ``send_timeout`` seconds to accept a message is dropped, and reconnects; one that
    stays silent is found by pings every ``heartbeat`` seconds; closing waits up to
    ``close_timeout`` seconds.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        send_timeout: float,
        heartbeat: float,
        close_timeout: float,
        ui_dir: str = "",
    ) -> None:
        if not 0 <= port <= 65535:
            raise ValueError(f"port must be from 0 to 65535, got {port}")
        self.send_timeout = positive("send_timeout", send_timeout)
        self.heartbeat = positive("heartbeat", heartbeat)
        self.close_timeout = positive("close_timeout", close_timeout)
        self.ui_dir = Path(ui_dir) if ui_dir else DEFAULT_UI_DIR
        if not (self.ui_dir / PAGE).is_file():
            raise ValueError(f"ui_dir: no {PAGE} in {self.ui_dir}")
        self.host = host
        self.port = port
        self.address: tuple[str, int] | None = None  # where it listens, once open
        self.revision = ""  # fingerprint of the renderer's files, once open
        self.state: ScreenState | None = None  # None until the first show()
        self.gaze: tuple[float, float] | None = None
        self.pages: dict[web.WebSocketResponse, dict[str, Any]] = {}  # page -> its "ready"
        self._events: asyncio.Queue[ScreenEvent] = asyncio.Queue()
        self._runner: web.AppRunner | None = None
        self._unknown: set[tuple[str, str]] = set()

    async def open(self) -> None:
        self.revision = await asyncio.to_thread(_revision, self.ui_dir)
        app = web.Application()
        app.router.add_get("/ws", self._connect)
        app.router.add_get("/", self._page)
        app.router.add_static("/", self.ui_dir)
        app.on_response_prepare.append(_no_cache)
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=self.close_timeout)
        await runner.setup()
        try:
            await web.TCPSite(runner, self.host, self.port).start()
        except BaseException:
            await runner.cleanup()
            raise
        self._runner = runner
        host, port = runner.addresses[0][:2]
        self.address = (host, port)
        log.info("screen at http://%s:%d/", host, port)

    async def close(self) -> None:
        for page in list(self.pages):
            await page.close(code=WSCloseCode.GOING_AWAY, message=b"robot stopping")
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def show(self, state: ScreenState) -> None:
        self.state = state
        self._check_names()
        await self._broadcast(self._state_message(state))

    async def look(self, target: tuple[float, float] | None) -> None:
        self.gaze = target
        await self._broadcast(self._look_message())

    async def voice(self, level: float) -> None:
        await self._broadcast({"type": "voice", "level": round(level, 3)})

    async def event(self) -> ScreenEvent:
        return await self._events.get()

    def _hello_message(self) -> dict[str, Any]:
        return {
            "type": "hello",
            "protocol": PROTOCOL,
            "revision": self.revision,
            "time": time.time(),
        }

    def _state_message(self, state: ScreenState) -> dict[str, Any]:
        scene = None if state.scene is None else {"name": state.scene, "data": state.scene_data}
        return {
            "type": "state",
            "face": {"expression": state.expression, "tone": state.tone},
            "scene": scene,
            "status": {
                "battery": state.battery,
                "charging": state.charging,
                "low": state.battery_low,
                "muted": state.muted,
            },
            "panel": panel_message(state.panel),
            "asleep": state.asleep,
            "time": time.time(),
        }

    def _look_message(self) -> dict[str, Any]:
        target = None if self.gaze is None else {"x": self.gaze[0], "y": self.gaze[1]}
        return {"type": "look", "target": target}

    async def _page(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.ui_dir / PAGE)

    async def _connect(self, request: web.Request) -> web.WebSocketResponse:
        page = web.WebSocketResponse(heartbeat=self.heartbeat, timeout=self.close_timeout)
        await page.prepare(request)
        self.pages[page] = {}
        try:
            # The whole state, so that a page that (re)connects shows the right thing. A
            # page that connects before the first show() waits for it, on its start-up.
            messages = [self._hello_message()]
            if self.state is not None:
                messages += [self._state_message(self.state), self._look_message()]
            for message in messages:
                await self._send(page, encode(message))
            async for message in page:
                if message.type == WSMsgType.TEXT:
                    self._receive(page, message.data)
        finally:
            self.pages.pop(page, None)
        return page

    def _receive(self, page: web.WebSocketResponse, text: str) -> None:
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            log.warning("screen page sent something that is not JSON")
            return
        kind = message.get("type") if isinstance(message, dict) else None
        if kind == "ready":
            self.pages[page] = message
            if message.get("protocol") != PROTOCOL:
                log.warning(
                    "screen page speaks protocol %r, not %d", message.get("protocol"), PROTOCOL
                )
            log.info(
                "screen page ready: %sx%s, %s",
                message.get("width"),
                message.get("height"),
                message.get("shape"),
            )
            self._check_names()
            self._events.put_nowait(Opened())
        elif kind in ("touch", "hold"):
            x, y = message.get("x"), message.get("y")
            if _fraction(x) and _fraction(y):
                event = Touch if kind == "touch" else Hold
                self._events.put_nowait(event(float(x), float(y)))
            else:
                log.warning("screen page sent a %s off the screen: %r", kind, message)
        elif kind == "action":
            panel, item, value = message.get("panel"), message.get("item"), message.get("value")
            if _name(panel) and _name(item) and _scalar(value):
                self._events.put_nowait(Action(panel, item, value))
            else:
                log.warning("screen page sent an action that is not one: %r", message)
        elif kind == "close":
            if _name(message.get("panel")):
                self._events.put_nowait(Close(message["panel"]))
            else:
                log.warning("screen page closed no panel: %r", message)

    def _check_names(self) -> None:
        """Warn once per name about an expression or a scene no ready page knows. A
        page that lists none of a kind (an older renderer) knows none of it."""
        state = self.state
        ready = [message for message in self.pages.values() if message]
        if state is None or not ready:
            return
        wanted = [("expressions", state.expression, "its default expression")]
        if state.scene is not None:
            wanted.append(("scenes", state.scene, "no scene"))
        for kind, name, instead in wanted:
            known = any(name in (page.get(kind) or ()) for page in ready)
            if not known and (kind, name) not in self._unknown:
                self._unknown.add((kind, name))
                log.warning("no screen page knows the %s %r; it shows %s", kind[:-1], name, instead)

    async def _broadcast(self, message: dict[str, Any]) -> None:
        if self.pages:
            text = encode(message)
            await asyncio.gather(*(self._send(page, text) for page in list(self.pages)))

    async def _send(self, page: web.WebSocketResponse, text: str) -> None:
        """Send ``text`` to ``page``; a page that cannot take it is dropped and reconnects."""
        try:
            async with asyncio.timeout(self.send_timeout):
                await page.send_str(text)
        except (TimeoutError, ConnectionError) as error:
            log.warning("dropping a screen page: %s", error or type(error).__name__)
            self.pages.pop(page, None)
            await page.close(code=WSCloseCode.GOING_AWAY, message=b"too slow")
