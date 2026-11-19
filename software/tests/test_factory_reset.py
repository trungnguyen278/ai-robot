# SPDX-License-Identifier: MIT
"""The factory reset (docs/decisions/0023-factory-reset.md): asked in the settings, the
robot leaves its home, erases itself even when it could not leave, and starts again; a
reset cut short goes on. Its adapters: files, and systemd on a private D-Bus with a fake
systemd (local_dbus.py). Only the owner's Erase starts a reset: not a stray file, a
value kept for it, nor a page from another site. And a revocation, which erases nothing
(docs/decisions/0022-a-removed-robot-keeps-its-data.md)."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import waiting
from local_dbus import impersonate, private_bus

from ai_robot.adapters.files import json_file, reset_record
from ai_robot.adapters.files.factory_reset import FileFactoryReset
from ai_robot.adapters.files.reset_record import Record
from ai_robot.adapters.sim.setup import SimSetupLink
from ai_robot.app import App
from ai_robot.core.bus import Subscription
from ai_robot.core.config import load_config
from ai_robot.messages import (
    BindingChanged,
    PanelAction,
    PanelRequest,
    ResetStage,
    Resetting,
    SceneRequest,
    ScreenHeld,
    ScreenTouched,
)
from ai_robot.modules.reset import ACTION, SCENE
from ai_robot.modules.screen import SLEEP_SETTING
from ai_robot.modules.settings import PANEL
from ai_robot.ports.factory_reset import Restart
from ai_robot.ports.screen import Panel

TIMEOUT = 10.0  # seconds any step may take
SERVER = "https://cloud.example"
LEAVING, NOT_LEFT, ERASING = ResetStage.LEAVING, ResetStage.NOT_LEFT, ResetStage.ERASING


def keep(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def kept(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


class Robot:
    """The desktop robot, keeping its state in files under ``folder``, as in a home: its
    state folder, and its key file in a folder of its own, beside an unrelated file; it
    resets with the ``files`` adapter."""

    def __init__(self, shipped_config: Path, folder: Path, *, note: float = 0.05) -> None:
        home = folder / "home"
        self.state = home / ".local" / "state" / "ai-robot"
        self.identity = home / ".config" / "ai-robot" / "identity.json"
        self.unrelated = home / ".config" / "notes.txt"
        self.record = self.state / "resetting.json"
        self.unrelated.parent.mkdir(parents=True)
        self.unrelated.write_text("mine", encoding="utf-8")
        table = load_config("desktop", shipped_config)
        ports = table["ports"]
        ports["screen"]["web"]["port"] = 0  # any free port
        for port in ("settings", "binding"):
            ports[port]["adapter"] = "file"
            ports[port]["file"]["path"] = str(self.state / f"{port}.json")
        ports["identity"]["adapter"] = "file"
        ports["identity"]["file"] = {"path": str(self.identity)}
        ports["factory_reset"]["adapter"] = "files"
        ports["factory_reset"]["files"].update(
            record=str(self.record), erase=[str(self.state), str(self.identity)]
        )
        table["modules"]["reset"]["note"] = note
        self.table = table

    def build(self) -> None:
        self.app = App(self.table)
        self.app.build()
        self.bus = self.app.bus
        self.cloud: Any = self.app.adapters["cloud"]
        self.link: SimSetupLink = self.app.adapters["setup_link"]
        self.resets = self.bus.subscribe(Resetting)
        self.panels = self.bus.subscribe(PanelRequest)
        self.scenes = self.bus.subscribe(SceneRequest)

    def drive(self, owner: Callable[[asyncio.Event], Awaitable[None]]) -> None:
        """Run the robot while ``owner`` acts on it; it ends when the robot restarts, or
        stops as ``owner`` asks."""

        async def main() -> None:
            stop = asyncio.Event()
            running = asyncio.create_task(self.app.run(stop))
            try:
                await asyncio.wait_for(owner(stop), TIMEOUT)
                await asyncio.wait_for(asyncio.shield(running), TIMEOUT)
            finally:
                stop.set()
                await running

        asyncio.run(main())

    async def open_settings(self) -> Panel:
        """Hold a finger on the face; the settings once they offer the reset."""
        await waiting.until(self.identity.exists, TIMEOUT)  # the robot is going
        self.bus.publish(ScreenHeld(0.5, 0.5))
        while True:
            request = await self.panels.get()
            if request.panel is not None and request.panel.item(ACTION) is not None:
                assert request.panel.name == PANEL
                return request.panel

    async def erase(self) -> None:
        """What the owner does: Settings, Reset to factory settings, then Erase."""
        await self.open_settings()
        self.bus.publish(PanelAction(PANEL, ACTION))

    def erased(self) -> bool:
        """The state folder is empty, the key file gone, and the unrelated file kept."""
        empty = self.state.is_dir() and not any(self.state.iterdir())
        return empty and not self.identity.exists() and self.unrelated.exists()


async def taken(subscription: Subscription[Any]) -> list[Any]:
    """The messages ``subscription`` holds now."""
    return [await subscription.get() for _ in range(subscription.pending)]


async def stages(robot: Robot) -> list[ResetStage]:
    return [message.stage for message in await taken(robot.resets)]


def scenes_of(requests: list[SceneRequest], source: str) -> list[Any]:
    return [(r.scene, r.data) for r in requests if r.source == source]


# ---- the robot ----


def test_the_owner_resets_the_robot_from_its_settings(shipped_config: Path, tmp_path: Path) -> None:
    robot = Robot(shipped_config, tmp_path)
    keep(robot.state / "binding.json", {"server": SERVER, "bound": True})
    keep(robot.state / "settings.json", {SLEEP_SETTING: 10})
    robot.build()

    async def owner(_: asyncio.Event) -> None:
        panel = await robot.open_settings()
        about = next(s for s in panel.sections if s.title == "About")
        item = about.items[-1]  # the About section ends with it
        assert (item.id, item.kind, item.label) == (ACTION, "button", "Reset to factory settings")
        assert item.confirm == "Reset to factory settings?"
        assert item.confirm_label == "Erase"
        assert "This cannot be undone." in item.confirm_lines
        assert any("Wi-Fi" in line and "name" in line for line in item.confirm_lines)
        robot.bus.publish(PanelAction(PANEL, ACTION))

    robot.drive(owner)
    assert robot.app.restart  # it starts again, as a new robot
    assert robot.cloud.left  # it left its home first
    assert robot.erased() and not robot.record.exists()

    async def seen() -> None:
        assert await stages(robot) == [LEAVING, ERASING]
        shown = scenes_of(await taken(robot.scenes), "reset")
        assert shown == [(SCENE, {"state": "leaving"}), (SCENE, {"state": "erasing"})]

    asyncio.run(seen())


def test_news_of_the_home_that_comes_late_is_not_kept(
    shipped_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cloud says it knows the robot again as the robot is erased: the binding does
    not come back."""
    erase = FileFactoryReset.erase

    async def erase_then_hear(reset: FileFactoryReset) -> Restart:
        restart = await erase(reset)
        robot.cloud.recognise()
        await asyncio.sleep(0.2)
        return restart

    monkeypatch.setattr(FileFactoryReset, "erase", erase_then_hear)
    robot = Robot(shipped_config, tmp_path)
    keep(robot.state / "binding.json", {"server": SERVER, "bound": True})
    robot.build()

    async def owner(_: asyncio.Event) -> None:
        await robot.erase()

    robot.drive(owner)
    assert robot.app.restart and robot.erased()


def test_a_robot_that_cannot_leave_says_so_then_erases(
    shipped_config: Path, tmp_path: Path
) -> None:
    """Offline, it erases anyway (ETSI EN 303 645, 5.11-1), its screen first saying to
    remove it in the app. Meanwhile set-up and the settings stay closed."""
    robot = Robot(shipped_config, tmp_path, note=1.0)
    keep(robot.state / "binding.json", {"server": SERVER, "bound": False})  # being set up
    robot.build()
    robot.cloud.knows_robot = False
    robot.cloud.answers_leave = False

    async def owner(_: asyncio.Event) -> None:
        await waiting.until(lambda: robot.link.offered, TIMEOUT)
        robot.link.connect("phone")  # an app is setting the robot up
        await robot.erase()
        await waiting.until(lambda: robot.resets.pending == 2, TIMEOUT)
        assert await stages(robot) == [LEAVING, NOT_LEFT]
        assert robot.record.exists()  # kept before it leaves: a reset cut short goes on
        await waiting.until(lambda: not robot.link.offered, TIMEOUT)
        assert not robot.link.connected("phone")
        assert scenes_of(await taken(robot.scenes), "setup")[-1][0] is None
        await taken(robot.panels)
        robot.bus.publish(ScreenHeld(0.5, 0.5))  # the settings stay closed
        robot.bus.publish(ScreenTouched(0.5, 0.5))  # and set-up too
        await waiting.until(lambda: robot.resets.pending == 1, TIMEOUT)
        assert await stages(robot) == [ERASING]
        assert all(request.panel is None for request in await taken(robot.panels))
        assert scenes_of(await taken(robot.scenes), "setup") == []
        assert not robot.link.offered

    robot.drive(owner)
    assert robot.app.restart and robot.erased()
    assert not robot.cloud.left


@pytest.mark.parametrize("server", [None, SERVER], ids=["never-set-up", "being-set-up"])
def test_a_reset_cut_short_goes_on_at_the_next_start(
    shipped_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: str | None
) -> None:
    """Set-up never opens, although the robot is not bound. Never set up, the robot is
    in no home; being set up, it asks to leave again, and cannot."""
    offers: list[str] = []
    start = SimSetupLink.start

    async def offer(link: SimSetupLink, name: str) -> None:
        offers.append(name)
        await start(link, name)

    monkeypatch.setattr(SimSetupLink, "start", offer)
    robot = Robot(shipped_config, tmp_path, note=0.5)
    keep(robot.record, {"resetting": True})
    keep(robot.state / "settings.json", {SLEEP_SETTING: 10})
    if server is not None:
        keep(robot.state / "binding.json", {"server": server, "bound": False})
    robot.build()
    robot.cloud.knows_robot = False
    robot.cloud.answers_leave = False

    async def owner(_: asyncio.Event) -> None:
        pass  # the robot goes on by itself

    robot.drive(owner)
    assert robot.app.restart and robot.erased() and not robot.record.exists()
    assert offers == []
    assert not robot.cloud.left

    async def seen() -> None:
        # never set up, it asked nothing of a cloud; being set up, the cloud did not answer
        left = [LEAVING, ERASING] if server is None else [LEAVING, NOT_LEFT, ERASING]
        assert await stages(robot) == left
        assert all(scene is None for scene, _ in scenes_of(await taken(robot.scenes), "setup"))

    asyncio.run(seen())


def test_a_value_kept_for_the_reset_does_not_reset(
    shipped_config: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The settings file holds the reset's name: only pressing it resets."""
    robot = Robot(shipped_config, tmp_path)
    keep(robot.state / "binding.json", {"server": SERVER, "bound": True})
    keep(robot.state / "settings.json", {ACTION: None})
    robot.build()

    async def owner(stop: asyncio.Event) -> None:
        await robot.open_settings()
        await waiting.until(lambda: "is an action" in caplog.text, TIMEOUT)
        stop.set()

    robot.drive(owner)
    assert robot.resets.pending == 0
    assert not (robot.app.restart or robot.cloud.left or robot.erased())


@pytest.mark.parametrize("content", [b"", b'{"resetting": 1}', None], ids=["empty", "1", "folder"])
def test_only_the_robots_own_record_resets_it(
    shipped_config: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture, content: bytes | None
) -> None:
    """``None``: a folder where the record goes."""
    robot = Robot(shipped_config, tmp_path)
    keep(robot.state / "binding.json", {"server": SERVER, "bound": True})
    if content is None:
        robot.record.mkdir(parents=True)
    else:
        robot.record.parent.mkdir(parents=True, exist_ok=True)
        robot.record.write_bytes(content)
    robot.build()

    async def owner(stop: asyncio.Event) -> None:
        await robot.open_settings()  # they stay closed once a reset began
        stop.set()

    robot.drive(owner)
    assert robot.resets.pending == 0
    assert not (robot.app.restart or robot.cloud.left or robot.erased())
    assert content is None or robot.record.read_bytes() == content
    assert str(robot.record) in caplog.text  # by its path, never what it holds


def test_a_page_from_another_site_cannot_reset_the_robot(
    shipped_config: Path, tmp_path: Path
) -> None:
    """It would hold the face, then press the reset, with no Erase on the screen."""
    robot = Robot(shipped_config, tmp_path)
    keep(robot.state / "binding.json", {"server": SERVER, "bound": True})
    robot.build()

    async def owner(stop: asyncio.Event) -> None:
        await waiting.until(robot.identity.exists, TIMEOUT)  # the robot is going
        host, port = robot.app.adapters["screen"].address
        foreign = {"Origin": "https://attacker.example"}
        async with aiohttp.ClientSession() as http:
            with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
                await http.ws_connect(f"http://{host}:{port}/ws", headers=foreign)
        assert refused.value.status == 403
        stop.set()

    robot.drive(owner)
    assert not (robot.app.restart or robot.cloud.left or robot.erased())


def test_a_revoked_robot_erases_nothing(shipped_config: Path, tmp_path: Path) -> None:
    """PROV-4: removed in the app, the robot keeps its settings, its identity and the
    server it knew; it offers set-up again, and only its own reset erases it."""
    robot = Robot(shipped_config, tmp_path)
    keep(robot.state / "binding.json", {"server": SERVER, "bound": True})
    keep(robot.state / "settings.json", {SLEEP_SETTING: 10})
    robot.build()
    bindings = robot.bus.subscribe(BindingChanged)

    async def owner(stop: asyncio.Event) -> None:
        await waiting.until(robot.identity.exists, TIMEOUT)
        identity = robot.identity.read_bytes()
        robot.cloud.forget()  # its owner removed it in the app
        while (await bindings.get()).bound is not False:
            pass
        await waiting.until(lambda: robot.link.offered, TIMEOUT)
        assert robot.identity.read_bytes() == identity
        stop.set()

    robot.drive(owner)
    assert not robot.app.restart
    assert robot.resets.pending == 0 and not robot.record.exists()
    assert kept(robot.state / "settings.json") == {SLEEP_SETTING: 10}
    assert kept(robot.state / "binding.json") == {"server": SERVER, "bound": False}
    assert robot.unrelated.exists()


# ---- the files adapter ----


@pytest.fixture
def home(tmp_path: Path) -> dict[str, Path]:
    """A robot's files: its state folder, which holds the record, its key file in a
    folder of its own, and a file it does not own."""
    state = tmp_path / ".local" / "state" / "ai-robot"
    files = {
        "state": state,
        "record": state / "resetting.json",
        "settings": state / "settings.json",
        "deep": state / "cache" / "deep" / "file.bin",
        "key": tmp_path / ".config" / "ai-robot" / "identity.json",
        "other": tmp_path / ".config" / "notes.txt",
    }
    for name in ("settings", "deep", "key", "other"):
        files[name].parent.mkdir(parents=True, exist_ok=True)
        files[name].write_text(name, encoding="utf-8")
    return files


def reset_of(home: dict[str, Path]) -> FileFactoryReset:
    return FileFactoryReset(
        record=str(home["record"]),
        erase=[str(home["state"]), str(home["key"]), str(home["state"] / "missing")],
        wifi=False,
        bus="unix:path=/nonexistent",  # never reached without wifi
    )


def test_the_files_adapter_erases_what_it_lists_and_its_record_last(
    home: dict[str, Path],
) -> None:
    reset = reset_of(home)

    async def main() -> None:
        assert not await reset.pending()
        await reset.begin()
        assert await reset.pending()
        assert await reset.erase() is Restart.PROCESS
        assert not await reset.pending()

    asyncio.run(main())
    assert home["state"].is_dir() and list(home["state"].iterdir()) == []  # it held the record
    assert not home["key"].exists()
    assert home["other"].read_text(encoding="utf-8") == "other"


def test_an_erase_cut_short_keeps_its_record(
    home: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    reset = reset_of(home)

    def fail(path: Path) -> None:
        raise OSError("the disk failed")

    async def main() -> None:
        await reset.begin()
        monkeypatch.setattr("shutil.rmtree", fail)
        with pytest.raises(OSError):
            await reset.erase()
        assert await reset.pending()  # it goes on at the next start
        monkeypatch.undo()
        await reset.erase()
        assert not await reset.pending()

    asyncio.run(main())
    assert not home["deep"].exists() and not home["key"].exists()


def test_the_record_goes_last_and_its_going_is_on_the_disk(
    home: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    reset = reset_of(home)
    asyncio.run(reset.begin())
    synced: list[tuple[Path, bool]] = []
    monkeypatch.setattr(
        json_file, "sync_folder", lambda folder: synced.append((folder, home["record"].exists()))
    )
    asyncio.run(reset.erase())
    assert synced == [(home["state"], False)]


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"{",
        b"null",
        b"[]",
        b"true",
        b'{"resetting": false}',
        b'{"resetting": 1}',
        b'{"resetting": "true"}',
        b'{"resetting": true, "erase": ["~"]}',
        b'{"boot": "b1"}',
        b'{"resetting": true, "boot": ""}',
        b'{"resetting": true, "boot": 7}',
        b'{"resetting": true, "resetting": true}',  # json keeps the last only
        pytest.param(b"[" * 100_000, id="deeper than Python parses"),
        b"garbage \x00\xff",
    ],
)
def test_only_the_robots_own_record_says_a_reset_began(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, content: bytes
) -> None:
    path = tmp_path / "resetting.json"
    path.write_bytes(content)
    assert reset_record.read(path) is None
    assert str(path) in caplog.text


def test_the_robots_own_record_says_a_reset_began(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "state" / "resetting.json"
    assert reset_record.read(path) is None  # no reset began
    for record in (Record(), Record("b1")):
        reset_record.write(path, record)
        assert reset_record.read(path) == record
    assert kept(path) == {"resetting": True, "boot": "b1"}
    assert not caplog.text


def test_a_link_goes_and_never_what_it_points_to(home: dict[str, Path], tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "photo.jpg").write_text("photo", encoding="utf-8")
    links = [home["state"] / "photos", home["key"].parent / "photos"]
    try:
        for link in links:
            link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("this system makes no symbolic links for this user")
    reset = FileFactoryReset(
        record=str(home["record"]),
        erase=[str(home["state"]), str(links[1])],
        wifi=False,
        bus="unix:path=/nonexistent",
    )
    asyncio.run(reset.erase())
    assert not any(os.path.lexists(link) for link in links)
    assert (outside / "photo.jpg").exists()


RECORD = "~/.local/state/ai-robot/resetting.json"  # as shipped
ERASE = ["~/.local/state/ai-robot", "~/.config/ai-robot/identity.json"]  # local.example.toml


@pytest.mark.parametrize(
    ("record", "erase"),
    [
        ("", []),
        ("resetting.json", []),  # where the robot happens to start
        ("~/.local/state/ai-robot/../resetting.json", []),
        ("~/resetting.json", []),  # in the home folder itself
        ("/resetting.json", []),  # in a folder that holds it
        (RECORD, [""]),
        (RECORD, ["~/.local/state/ai-robot", 3]),
        (RECORD, "~/.local/state/ai-robot"),
        (RECORD, ["~"]),  # the whole home folder
        (RECORD, ["/"]),
        (RECORD, ["~/.config"]),
        (RECORD, ["~/notes.txt"]),
        (RECORD, ["ai-robot/identity.json"]),
        (RECORD, ["~/.config/ai-robot/../notes.txt"]),
    ],
)
def test_the_files_adapter_erases_only_what_the_robot_owns(record: str, erase: Any) -> None:
    FileFactoryReset(record=RECORD, erase=ERASE, wifi=False, bus="system")
    with pytest.raises(ValueError):
        FileFactoryReset(record=record, erase=erase, wifi=False, bus="system")


def test_a_user_named_as_the_robot_keeps_their_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "ai-robot"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    record = str(home / ".local" / "state" / "ai-robot" / "resetting.json")
    own = str(home / ".config" / "ai-robot")
    FileFactoryReset(record=record, erase=[own], wifi=False, bus="system")
    for erase in (str(home), str(home / ".config")):
        with pytest.raises(ValueError):
            FileFactoryReset(record=record, erase=[erase], wifi=False, bus="system")


# ---- on D-Bus (Linux) ----


@pytest.fixture
def bus(tmp_path: Path) -> Iterator[str]:
    with private_bus(tmp_path) as address:
        yield address


def test_the_files_adapter_forgets_only_the_networks_set_up_joined(
    bus: str, home: dict[str, Path]
) -> None:
    from fake_networkmanager import FakeNetworkManager

    async def main() -> None:
        manager = FakeNetworkManager(bus, [])
        await manager.start()
        try:
            joined = manager.save(b"Home", "secret", {"ai-robot.setup": "joined"})
            theirs = manager.save(b"Office", "secret")  # its owner made it
            tools = manager.save(b"Cafe", None, {"other.tool": "joined"})  # another program did
            reset = FileFactoryReset(
                record=str(home["record"]), erase=[str(home["state"])], wifi=True, bus=bus
            )
            await reset.begin()
            await reset.erase()
            assert set(manager.profiles) == {theirs, tools}
            assert joined not in manager.profiles
        finally:
            await manager.stop()

    asyncio.run(main())
    assert not home["record"].exists()


BOOT = "5c4f2fbd-9e2c-4a8e-8f53-4b1f3b0e6a10"  # this boot's ID, as the kernel gives it


@asynccontextmanager
async def systemd_on(bus: str, folder: Path) -> AsyncIterator[Any]:
    """systemd, on the bus and with its factory reset service in ``folder``; this boot's
    ID in ``folder``."""
    from fake_systemd import FakeFactoryResetService, FakeSystemd

    (folder / "boot_id").write_text(BOOT + "\n", encoding="ascii")
    manager = FakeSystemd(bus)
    manager.service = FakeFactoryResetService(folder / "varlink")
    await manager.start()
    await manager.service.start()
    try:
        yield manager
    finally:
        await manager.service.stop()
        await manager.stop()


def systemd_reset(bus: str, folder: Path) -> Any:
    from ai_robot.adapters.systemd.factory_reset import SystemdFactoryReset

    return SystemdFactoryReset(
        record=str(folder / "state" / "resetting.json"),
        bus=bus,
        varlink=str(folder / "varlink"),
        boot_id=str(folder / "boot_id"),
    )


@pytest.mark.parametrize("early", [False, True])
def test_the_image_resets_through_systemd(bus: str, tmp_path: Path, early: bool) -> None:
    """``early``: systemd says the job ended before it answers the call that started it."""

    async def main() -> None:
        async with systemd_on(bus, tmp_path) as systemd:
            systemd.early = early
            reset = systemd_reset(bus, tmp_path)
            await reset.begin()
            assert await asyncio.wait_for(reset.erase(), TIMEOUT) is Restart.SYSTEM
            assert systemd.service.calls == ["io.systemd.FactoryReset.GetFactoryResetMode"]
            assert systemd.calls == [
                ("Subscribe", []),
                ("StartUnit", ["factory-reset.target", "replace"]),
            ]
            assert await reset.pending()  # the image erases the record with the rest
            assert kept(reset.record) == {"resetting": True, "boot": BOOT}

    asyncio.run(main())


@pytest.mark.parametrize("mode", ["unspecified", "off", "pending"])
def test_the_robot_asks_once_in_a_boot_whatever_comes_of_it(
    bus: str, tmp_path: Path, mode: str
) -> None:
    """The record says the boot it asked in before systemd hears of it; asked again in
    the same boot, as when the robot restarts before the system does, it asks again."""

    async def main() -> None:
        async with systemd_on(bus, tmp_path) as systemd:
            systemd.service.mode = mode
            systemd.hold = True
            reset = systemd_reset(bus, tmp_path)
            await reset.begin()
            erasing = asyncio.create_task(reset.erase())
            await asyncio.wait_for(systemd.started.wait(), TIMEOUT)
            assert kept(reset.record) == {"resetting": True, "boot": BOOT}
            systemd.finish("done")
            await asyncio.wait_for(erasing, TIMEOUT)
            systemd.hold = False
            assert await asyncio.wait_for(reset.erase(), TIMEOUT) is Restart.SYSTEM

    asyncio.run(main())


def test_a_system_that_restarted_without_erasing_is_not_asked_again(
    bus: str, tmp_path: Path
) -> None:
    """Ubuntu's target on a Pi restarts it with no request for the next boot: the robot
    would restart it again and again."""

    async def main() -> None:
        async with systemd_on(bus, tmp_path) as systemd:
            reset = systemd_reset(bus, tmp_path)
            keep(reset.record, {"resetting": True, "boot": "an earlier boot"})
            assert await reset.pending()
            with pytest.raises(RuntimeError, match="restarted without erasing"):
                await asyncio.wait_for(reset.erase(), TIMEOUT)
            assert systemd.calls == []

    asyncio.run(main())


@pytest.mark.parametrize(
    ("mode", "error"),
    [
        ("unsupported", "offers no factory reset"),
        ("on", "left the robot's data"),  # this boot is the reset's, and the record is here
        ("complete", "left the robot's data"),
        ("hesitant", "no known mode"),
    ],
)
def test_the_robot_asks_only_a_system_that_can_reset(
    bus: str, tmp_path: Path, mode: str, error: str
) -> None:
    async def main() -> None:
        async with systemd_on(bus, tmp_path) as systemd:
            systemd.service.mode = mode
            reset = systemd_reset(bus, tmp_path)
            await reset.begin()
            with pytest.raises(RuntimeError, match=error):
                await asyncio.wait_for(reset.erase(), TIMEOUT)
            assert systemd.calls == []
            assert kept(reset.record) == {"resetting": True}

    asyncio.run(main())


UNREACHED = "cannot be reached"
UNKNOWN = "in no known mode"


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        (b'{"error": "io.systemd.System", "parameters": {}}\0', UNREACHED),
        (b"[1]\0", UNREACHED),
        (b'{"parameters": [1]}\0', UNREACHED),
        (b'{"parameters": {"mode": 3}}\0', UNKNOWN),
        (b'{"parameters": {"mode": ["on"]}}\0', UNKNOWN),
        (b"not json\0", UNREACHED),
        (b'{"parameters": {"mode": "on", "mode": "unspecified"}}\0', UNREACHED),
        (b"[" * 50_000 + b"\0", UNREACHED),  # deeper than Python parses, within the limit
        (b"", UNREACHED),  # it hangs up
        (None, UNREACHED),  # nothing listens
    ],
    ids=[
        "error",
        "list",
        "list of parameters",
        "number",
        "list mode",
        "garbage",
        "two modes",
        "deep",
        "gone",
        "absent",
    ],
)
def test_a_factory_reset_service_that_does_not_answer_is_no_yes(
    bus: str, tmp_path: Path, reply: bytes | None, error: str
) -> None:
    async def main() -> None:
        async with systemd_on(bus, tmp_path) as systemd:
            if reply is None:
                await systemd.service.stop()
                (tmp_path / "varlink").unlink(missing_ok=True)
            systemd.service.reply = reply
            reset = systemd_reset(bus, tmp_path)
            await reset.begin()
            with pytest.raises(RuntimeError, match=error):
                await asyncio.wait_for(reset.erase(), TIMEOUT)
            assert systemd.calls == []

    asyncio.run(main())


@pytest.mark.parametrize("result", ["failed", "canceled", "dependency", "timeout", "skipped"])
def test_a_reset_the_system_cannot_start_is_an_error(
    bus: str, tmp_path: Path, result: str, caplog: pytest.LogCaptureFixture
) -> None:
    async def main() -> None:
        async with systemd_on(bus, tmp_path) as systemd:
            systemd.result = result
            with pytest.raises(RuntimeError, match="could not start") as raised:
                await asyncio.wait_for(systemd_reset(bus, tmp_path).erase(), TIMEOUT)
            assert result not in str(raised.value)  # the error names no value: the log does
            assert f"ended: {result}" in caplog.text

    asyncio.run(main())


def test_the_robot_waits_for_its_own_job(bus: str, tmp_path: Path) -> None:
    async def main() -> None:
        async with systemd_on(bus, tmp_path) as systemd:
            systemd.hold = True
            erasing = asyncio.create_task(systemd_reset(bus, tmp_path).erase())
            await asyncio.wait_for(systemd.started.wait(), TIMEOUT)
            systemd.another_job_ends("apt-daily.service", "done")
            await asyncio.sleep(0.2)
            assert not erasing.done()
            systemd.finish("done")
            assert await asyncio.wait_for(erasing, TIMEOUT) is Restart.SYSTEM

    asyncio.run(main())


def test_only_systemd_says_the_reset_started(bus: str, tmp_path: Path) -> None:
    """Another program's signal, sent to the robot alone, does not end the job."""
    from dbus_fast import Message
    from fake_systemd import MANAGER, SYSTEMD_PATH

    async def main() -> None:
        async with systemd_on(bus, tmp_path) as systemd:
            systemd.hold = True
            erasing = asyncio.create_task(systemd_reset(bus, tmp_path).erase())
            await asyncio.wait_for(systemd.started.wait(), TIMEOUT)
            done = [1, f"{SYSTEMD_PATH}/job/1", "factory-reset.target", "done"]
            signal = Message.new_signal(SYSTEMD_PATH, MANAGER, "JobRemoved", "uoss", done)
            signal.destination = systemd.caller
            await impersonate(bus, signal)
            systemd.finish("failed")
            with pytest.raises(RuntimeError, match="could not start"):
                await asyncio.wait_for(erasing, TIMEOUT)

    asyncio.run(main())
