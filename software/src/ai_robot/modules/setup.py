# SPDX-License-Identifier: MIT
"""Set-up: the owner sets the robot up from the app, over Bluetooth LE
(platform/protocols/setup/README.md; docs/decisions/0015-set-up.md)."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from collections.abc import Awaitable
from typing import Any, TypeVar

from ai_robot import software_version
from ai_robot.core.checks import positive
from ai_robot.core.config import platform_dir
from ai_robot.core.module import Module, ModuleContext
from ai_robot.core.settings import SettingsHost
from ai_robot.core.setup_session import (
    CODE_DIGITS,
    DISCRIMINATOR_DIGITS,
    SSID_BYTES,
    Ended,
    Messages,
    Refusal,
    Secret,
    Session,
    SetupSchema,
    Stage,
    draw_discriminator,
    new_secret,
    setup_string,
    ssid_bytes,
    ssid_fields,
)
from ai_robot.messages import (
    BindingChanged,
    LocaleApplied,
    LocaleChanged,
    LocaleRequested,
    RegistrationRequested,
    RegistrationSigned,
    SceneRequest,
    ScreenPriority,
    ScreenTouched,
    Setting,
    SettingKind,
    SettingSection,
    SettingsOpened,
)
from ai_robot.ports.network import JoinError, Network, NetworkState, NetworkStatus, Security
from ai_robot.ports.setup import Connected, Received, SetupLink

SCENE = "setup"  # the screen's set-up scene (protocol/screen/vocabulary.json)
LINK_LENGTH = 256  # characters the scene's link may have, as in the vocabulary
MAX_NETWORKS = 50  # fixed by the protocol: the networks an answer lists, at most
LINK_PLACEHOLDER = "{setup}"  # where the setting link takes the set-up string
T = TypeVar("T")

_NOTHING_YET = object()  # what the screen was asked to show before anything

# How this implementation works, which nobody needs to tune.
REGISTRATION_TIMEOUT = 10.0  # seconds cloud has to sign a registration
CONFIGURE_TIMEOUT = 10.0  # seconds system has to take the languages and the time zone


def _load_schema() -> SetupSchema:
    path = platform_dir() / "setup" / "schema.json"
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot read the set-up protocol's schema: {error}") from None
    return SetupSchema(schema)


def _hidden(name: bytes) -> bool:
    """Whether a scan found ``name`` for a hidden network: no bytes, NUL bytes alone, as
    hostapd's ``ignore_broadcast_ssid=2`` sends, or a single space, as NetworkManager
    reads them all (``_nm_utils_is_empty_ssid_arr``, ``src/libnm-glib-aux/nm-shared-utils.c``
    on its ``main``, read 2026-11-13)."""
    return not name.strip(b"\0") or name == b" "


def _network_fields(status: NetworkStatus) -> dict[str, Any]:
    """How the protocol gives the network the robot is on."""
    if status.state is not NetworkState.CONNECTED:
        return {"state": status.state.value}
    if status.ssid is None or status.address is None:  # not quite connected yet
        return {"state": NetworkState.CONNECTING.value}
    return {
        "state": status.state.value,
        **ssid_fields(status.ssid),
        "address": status.address,
        "internet": status.internet,
    }


class SetupModule(Module):
    """Opens set-up, shows the code, and serves the app's session
    (platform/protocols/setup/README.md).

    - **When.** Set-up opens when the robot is unbound, as ``cloud`` says
      (``BindingChanged``): at start, or when the server stops accepting it. It also
      opens from the settings (``setup.start``), and, while the robot is unbound, on a
      tap on the screen. It closes after ``window`` seconds without a session, when the
      app finishes, or after ``attempts`` failed attempts.
    - **Settings** (docs/decisions/0016): the action that opens set-up, and the network
      the robot is on, read again each time the settings open.
    - **The code.** Each opening draws a discriminator and a code; each attempt with
      the code, successful or not, draws a new code. The screen's ``setup`` scene shows
      them, and ``link`` wraps the set-up string for the QR code.
    - **The session.** One app at a time: the first whose ``hello`` comes. Another app's
      ``hello`` gets the error ``busy``, which reaches that app only, and the robot drops
      it; an app that connects once set-up has closed is dropped with no message. An app
      that sends no whole message for ``idle`` seconds is dropped. Joining a network may
      take ``join_timeout`` seconds.
    - **The registration.** ``cloud``, which owns the robot's key, signs it
      (``RegistrationRequested``, ``RegistrationSigned``). Once the app finishes, the
      screen shows that the robot is connecting, until the server accepts it, or for
      ``bind_timeout`` seconds.
    - **The first settings.** ``system`` takes the languages and the time zone the app
      offers (``LocaleRequested``, ``LocaleApplied``), and tells them (``LocaleChanged``)
      for ``info``.
    """

    owns = ("setup_link", "network")

    def __init__(
        self,
        ctx: ModuleContext,
        *,
        window: float,
        attempts: int,
        idle: float,
        join_timeout: float,
        bind_timeout: float,
        link: str,
    ) -> None:
        super().__init__(ctx)
        self.window = positive("window", window)
        if attempts < 1:
            raise ValueError(f"attempts must be at least 1, got {attempts}")
        self.attempts = attempts
        self.idle = positive("idle", idle)
        self.join_timeout = positive("join_timeout", join_timeout)
        self.bind_timeout = positive("bind_timeout", bind_timeout)
        if LINK_PLACEHOLDER not in link:
            raise ValueError(f"link must contain {LINK_PLACEHOLDER}, got {link!r}")
        longest = link.replace(
            LINK_PLACEHOLDER, setup_string("F" * DISCRIMINATOR_DIGITS, "0" * CODE_DIGITS)
        )
        if len(longest) > LINK_LENGTH:
            raise ValueError(
                f"link gives the QR code {len(longest)} characters; the screen takes {LINK_LENGTH}"
            )
        self.link_template = link
        self.link = self.port("setup_link", SetupLink)
        self.network = self.port("network", Network)
        self._bindings = self.bus.subscribe(BindingChanged)
        self._touches = self.bus.subscribe(ScreenTouched)
        self._settings_opened = self.bus.subscribe(SettingsOpened)
        self.settings = SettingsHost(self)
        self.settings.add(
            Setting(
                "setup.start",
                SettingKind.ACTION,
                SettingSection.NETWORK,
                "Set up with the app",
                help="For a new Wi-Fi network or a new phone: shows a code to scan",
                closes=True,
            ),
            self._start_from_settings,
        )
        self._signatures = self.bus.subscribe(RegistrationSigned)
        self._locales = self.bus.subscribe(LocaleChanged)
        self._applied = self.bus.subscribe(LocaleApplied)
        self.locale: LocaleChanged | None = None  # the robot's languages and time zone
        self._schema: SetupSchema | None = None
        self.robot_id: str | None = None
        self.bound: bool | None = None  # None until cloud says
        self.is_open = False
        self._deadline = 0.0  # when set-up closes, if no session runs
        self._connecting_until: float | None = None  # after a registration, while unbound
        self._registered = False  # a registration was signed during this opening
        self.failures = 0
        self.discriminator = ""
        self._secret: Secret | None = None
        self.peer: str | None = None  # the app whose session runs
        self._session: Session | None = None
        # The messages of each app connected now, whole, then None once it has left.
        self._inboxes: dict[str, asyncio.Queue[bytes | None]] = {}
        self._apps: set[asyncio.Task[None]] = set()  # each serves one app connected
        self._pending: dict[str, asyncio.Future[RegistrationSigned]] = {}
        self._configuring: dict[str, asyncio.Future[LocaleApplied]] = {}
        self._numbers = itertools.count(1)
        self._changed = asyncio.Event()  # wakes the timer
        self._opening = asyncio.Lock()  # one opening or closing at a time
        self._shown: object = _NOTHING_YET  # what the screen was last asked to show

    async def start(self) -> None:
        self._schema = await asyncio.to_thread(_load_schema)
        self.settings.add(
            Setting(
                "setup.network",
                SettingKind.INFO,
                SettingSection.NETWORK,
                "Wi-Fi",
                await self._network_text(),
            )
        )
        self.settings.offer()

    async def run(self) -> None:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self._watch_binding())
            tasks.create_task(self._watch_settings())
            tasks.create_task(self._watch_touches())
            tasks.create_task(self.settings.serve())
            tasks.create_task(self._watch_signatures())
            tasks.create_task(self._watch_locale())
            tasks.create_task(self._watch_link())
            tasks.create_task(self._watch_time())

    async def stop(self) -> None:
        self.settings.close()
        for app in list(self._apps):
            app.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await app
        if self.is_open:
            await self._link("stop the set-up service", self.link.stop())

    async def _link(self, what: str, call: Awaitable[None]) -> bool:
        """Ask the link for something. A failure, such as Bluetooth turned off, is logged:
        set-up goes on without it, and the rest of the robot with it."""
        try:
            await call
        except (ConnectionError, OSError) as error:
            self.log.error("set-up cannot %s: %s", what, error)
            return False
        return True

    async def _network(self, call: Awaitable[T]) -> T:
        """Ask the network for something; the app hears of a failure."""
        try:
            return await call
        except (ConnectionError, OSError) as error:
            self.log.error("set-up cannot reach the robot's network service: %s", error)
            raise Refusal("network", "the robot's network service failed") from None

    # ---- opening and closing ----

    def _now(self) -> float:
        return asyncio.get_running_loop().time()

    async def _open(self) -> None:
        if self.robot_id is None:  # cloud has not said who the robot is yet
            return
        async with self._opening:
            if not self.is_open:
                self.discriminator = draw_discriminator()
                self._secret = await new_secret()
                self.failures = 0
                self._registered = False
                self._connecting_until = None
                offered = self.link.start(self.robot.display_name)
                if not await self._link("offer the set-up service", offered):
                    self._secret = None
                    self._show()
                    return
                self.is_open = True
                self.log.info("set-up is open")
                self._show()
            self._deadline = self._now() + self.window
            self._changed.set()

    async def _close(self) -> None:
        async with self._opening:
            if not self.is_open:
                return
            self.is_open = False
            self._secret = None
            if self._registered and not self.bound:
                self._connecting_until = self._now() + self.bind_timeout
            await self._link("stop the set-up service", self.link.stop())
            self.log.info("set-up is closed")
            self._show()
            self._changed.set()

    def _show(self) -> None:
        """What the screen shows of set-up (PROV-3: an unbound robot says so)."""
        data: dict[str, Any] | None
        if self.is_open and self._session is not None and self._session.stage is Stage.OPEN:
            data = {"state": "connected"}
        elif self.is_open and self._secret is not None:
            text = setup_string(self.discriminator, self._secret.code)
            data = {
                "state": "code",
                "code": self._secret.code,
                "discriminator": self.discriminator,
                "link": self.link_template.replace(LINK_PLACEHOLDER, text),
            }
        elif self.bound is False and self._connecting_until is not None:
            data = {"state": "connecting"}
        elif self.bound is False:
            data = {"state": "closed"}
        else:
            data = None
        if data == self._shown:
            return
        self._shown = data
        if data is None:
            self.bus.publish(SceneRequest(self.name, None))
        else:
            self.bus.publish(SceneRequest(self.name, SCENE, data, priority=ScreenPriority.ALERT))

    # ---- what changes it ----

    async def _watch_binding(self) -> None:
        while True:
            change = await self._bindings.get()
            was = self.bound
            self.robot_id, self.bound = change.robot, change.bound
            if change.bound:
                self._connecting_until = None
                if self.is_open and self._session is None:
                    await self._close()
            elif was is not False:  # unbound now: set-up opens by itself
                await self._open()
            self._show()

    async def _watch_settings(self) -> None:
        """Each time the settings open, read the network again for them."""
        while True:
            await self._settings_opened.get()
            self.settings.update("setup.network", await self._network_text())

    async def _start_from_settings(self, _: None) -> None:
        await self._open()

    async def _network_text(self) -> str:
        """The network the robot is on, as the settings show it."""
        try:
            status = await self.network.status()
        except (ConnectionError, OSError) as error:
            self.log.error("set-up cannot reach the robot's network service: %s", error)
            return "Unknown"
        if status.state is NetworkState.CONNECTING:
            return "Connecting"
        if status.state is not NetworkState.CONNECTED or status.ssid is None:
            return "Not connected"
        name = status.ssid.decode("utf-8", errors="replace")
        return f"{name}, no Internet" if status.internet is False else f"{name}, connected"

    async def _watch_touches(self) -> None:
        while True:
            await self._touches.get()
            if self.bound is False and not self.is_open and self._connecting_until is None:
                await self._open()

    async def _watch_signatures(self) -> None:
        while True:
            signed = await self._signatures.get()
            future = self._pending.get(signed.request)
            if future is not None and not future.done():
                future.set_result(signed)

    async def _watch_locale(self) -> None:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self._keep_locale())
            tasks.create_task(self._watch_applied())

    async def _keep_locale(self) -> None:
        while True:
            self.locale = await self._locales.get()

    async def _watch_applied(self) -> None:
        while True:
            applied = await self._applied.get()
            future = self._configuring.get(applied.request)
            if future is not None and not future.done():
                future.set_result(applied)

    async def _watch_time(self) -> None:
        """Closes set-up when its time is up, and ends the wait for the server."""
        while True:
            self._changed.clear()
            now = self._now()
            timeout: float | None = None
            if self.is_open and self._session is None:
                if now >= self._deadline:
                    await self._close()
                    continue
                timeout = self._deadline - now
            elif not self.is_open and self._connecting_until is not None:
                if now >= self._connecting_until:
                    self._connecting_until = None
                    self.log.warning("the robot's server has not accepted it since set-up")
                    self._show()
                    continue
                timeout = self._connecting_until - now
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), timeout)

    async def _watch_link(self) -> None:
        while True:
            event = await self.link.event()
            if isinstance(event, Connected):
                if not self.is_open:
                    # No message: busy would send the person to wait, where the robot
                    # needs opening again (the protocol's When set-up is open).
                    await self._link("drop an app", self.link.drop(event.peer))
                    continue
                inbox: asyncio.Queue[bytes | None] = asyncio.Queue()
                self._inboxes[event.peer] = inbox
                app = asyncio.create_task(self._greet(event.peer, inbox), name="set-up app")
                self._apps.add(app)
                app.add_done_callback(self._apps.discard)
            elif isinstance(event, Received):
                if event.peer in self._inboxes:
                    self._inboxes[event.peer].put_nowait(event.message)
            elif (left := self._inboxes.pop(event.peer, None)) is not None:
                left.put_nowait(None)

    async def _greet(self, peer: str, inbox: asyncio.Queue[bytes | None]) -> None:
        """Serve an app that connected, from its first whole message, its ``hello``: the
        session, if none runs; otherwise ``busy``, to this app only, and the robot drops
        it, while the session goes on (the protocol's Errors)."""
        try:
            first = await asyncio.wait_for(inbox.get(), self.idle)
        except TimeoutError:
            self.log.info("an app sent nothing to set-up, and was dropped")
            await self._link("drop an app", self.link.drop(peer))
            return
        if first is None:  # it left
            return
        if not self.is_open:  # it connected as set-up closed, and stayed
            await self._link("drop an app", self.link.drop(peer))
        elif self.peer is None and self._secret is not None:
            self.peer = peer
            await self._link("stop advertising", self.link.advertise(False))
            await self._serve(peer, self._secret, inbox, first)
        else:
            await self._refuse(peer, first)

    async def _refuse(self, peer: str, first: bytes) -> None:
        """Answer the first message of an app that finds set-up busy, then drop it."""
        assert self._schema is not None
        messages = Messages(self._schema)
        try:
            hello = messages.read(first)
            if hello["type"] != "hello":
                raise Refusal("protocol", f"expected hello, got {hello['type']}")
            refusal = Refusal("busy", "another set-up session runs")
        except Refusal as refused:
            refusal = refused
        await self._link("answer an app", self.link.send(peer, messages.error(refusal)))
        await self._link("drop an app", self.link.drop(peer))

    # ---- one session ----

    async def _serve(
        self, peer: str, secret: Secret, inbox: asyncio.Queue[bytes | None], first: bytes
    ) -> None:
        assert self._schema is not None
        session = Session(secret, self.discriminator, self._schema)
        self._session = session
        finished = False
        try:
            finished = await self._converse(peer, session, inbox, first)
        except Exception:  # set-up must outlive a failing session
            self.log.exception("a set-up session failed")
        finally:
            await self._after(peer, session, finished)

    async def _converse(
        self, peer: str, session: Session, inbox: asyncio.Queue[bytes | None], first: bytes
    ) -> bool:
        """Serve the session, from the app's ``first`` message, until it ends. Returns
        whether the app finished. Only whole messages keep it from being idle."""
        data: bytes | None = first
        while True:
            if data is None:  # the app left
                return False
            try:
                reply, request = session.receive(data)
                if reply is not None:
                    await self.link.send(peer, reply)
                elif request is None:  # the exchange is done
                    self.log.info("an app opened a set-up session")
                    self._show()
                if request is not None:
                    answer = await self._answer(session, request)
                    await self.link.send(peer, session.pack(answer))
                    if request["type"] == "finish":
                        return True
            except Ended as ended:
                self.log.info("%s", ended)
                return False
            except Refusal as refusal:
                await self.link.send(peer, session.error(refusal))
                if refusal.fatal:
                    self.log.info("a set-up session ended: %s", refusal.message)
                    return False
            try:
                data = await asyncio.wait_for(inbox.get(), self.idle)
            except TimeoutError:
                self.log.info("a set-up session was idle, and ended")
                return False

    async def _after(self, peer: str, session: Session, finished: bool) -> None:
        """Once a session ends: count a failed attempt, draw a new code, close set-up or
        advertise again."""
        self._session = None
        failed = session.attempted and session.stage is not Stage.OPEN
        if failed:
            self.failures += 1
            self.log.warning("a set-up attempt failed (%d so far)", self.failures)
        await self._link("drop an app", self.link.drop(peer))
        if self.is_open and session.attempted and not finished:
            self._secret = await new_secret()  # each code serves one attempt
        self.peer = None
        if finished or self.failures >= self.attempts:
            await self._close()
        elif self.is_open:
            await self._link("advertise", self.link.advertise(True))
            self._show()
        self._changed.set()

    async def _answer(self, session: Session, request: dict[str, Any]) -> dict[str, Any]:
        kind, reply_to = request["type"], request["seq"]
        if kind == "info":
            if self.locale is None:
                raise Refusal("protocol", "the robot's languages and time zone are not known yet")
            return session.message(
                "info",
                reply_to,
                device={
                    "id": self.robot_id,
                    "name": self.robot.display_name,
                    "kind": self.robot.kind,
                    "model": self.robot.model,
                    "software": software_version(),
                    "languages": list(self.locale.languages),
                    "time_zone": self.locale.time_zone,
                },
                bound=self.bound is True,
                network=_network_fields(await self._network(self.network.status())),
            )
        if kind == "networks":
            return session.message("networks", reply_to, networks=await self._networks())
        if kind == "join":
            return session.message("joined", reply_to, network=await self._join(request))
        if kind == "configure":
            applied = await self._configure(request)
            return session.message(
                "configured",
                reply_to,
                languages=list(applied.languages),
                time_zone=applied.time_zone,
            )
        if kind == "register":
            registration = await self._register(request)
            return session.message("registration", reply_to, registration=registration)
        if kind == "finish":
            return session.message("finished", reply_to)
        if kind in ("hello", "share", "confirm"):
            raise Refusal("protocol", f"{kind} after the exchange", fatal=True)
        raise Refusal("unknown", f"the robot does not know the request {kind}")

    async def _networks(self) -> list[dict[str, Any]]:
        found = []
        for network in await self._network(self.network.scan()):
            if _hidden(network.ssid) or len(network.ssid) > SSID_BYTES:
                continue
            fields = {"security": network.security.value, "signal": network.signal}
            found.append({**ssid_fields(network.ssid), **fields, "saved": network.saved})
            if len(found) == MAX_NETWORKS:
                break
        return found

    async def _join(self, request: dict[str, Any]) -> dict[str, Any]:
        name = ssid_bytes(request)
        security = Security(request["security"]) if "security" in request else None
        joining = self.network.join(
            name,
            request.get("password"),
            hidden=request.get("hidden", False),
            security=security,
            timeout=self.join_timeout,
        )
        try:
            status = await self._network(joining)
        except JoinError as error:
            raise Refusal(error.failure.value, str(error)) from None
        self.log.info("the robot joined a network")  # never its name or its password
        return _network_fields(status)

    async def _configure(self, request: dict[str, Any]) -> LocaleApplied:
        number = f"setup-{next(self._numbers)}"
        future: asyncio.Future[LocaleApplied] = asyncio.get_running_loop().create_future()
        self._configuring[number] = future
        try:
            languages = request.get("languages")
            self.bus.publish(
                LocaleRequested(
                    number,
                    None if languages is None else tuple(languages),
                    request.get("time_zone"),
                )
            )
            try:
                applied = await asyncio.wait_for(future, CONFIGURE_TIMEOUT)
            except TimeoutError:
                raise Refusal("time_zone", "the robot did not take its settings in time") from None
        finally:
            del self._configuring[number]
        if applied.error is not None:
            raise Refusal(applied.error, applied.detail or "the robot could not take its settings")
        return applied

    async def _register(self, request: dict[str, Any]) -> str:
        if self.bound:
            raise Refusal("bound", "the robot is bound: a new owner needs a reset")
        number = f"setup-{next(self._numbers)}"
        future: asyncio.Future[RegistrationSigned] = asyncio.get_running_loop().create_future()
        self._pending[number] = future
        try:
            self.bus.publish(RegistrationRequested(number, request["challenge"], request["server"]))
            try:
                signed = await asyncio.wait_for(future, REGISTRATION_TIMEOUT)
            except TimeoutError:
                raise Refusal("identity", "the robot did not sign in time") from None
        finally:
            del self._pending[number]
        if signed.registration is None:
            raise Refusal(signed.error or "identity", signed.detail or "the robot could not sign")
        self._registered = True
        return signed.registration
