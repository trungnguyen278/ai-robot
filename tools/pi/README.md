# tools/pi/ — the robot's Raspberry Pi

What the robot's Pi needs beyond Ubuntu 26.04's own image ([ADR-0017](../../docs/decisions/0017-ros-2.md)). The Pi's facts are in [`hardware/components/raspberry-pi-5.md`](../../hardware/components/raspberry-pi-5.md), and the checklist for changing its set-up in [`.claude/skills/pi5-platform`](../../.claude/skills/pi5-platform/SKILL.md).

## A patched BlueZ

Ubuntu 26.04's `bluez` 5.85 has a bug: when an app that is not bonded disconnects, BlueZ closes the notification channel of the app that subscribed first, instead of the one leaving. A second app turned away from set-up then ended the first app's session.

The robot therefore runs Ubuntu's `bluez`, rebuilt with upstream's fix, until Ubuntu's own package has it ([ADR-0020](../../docs/decisions/0020-patched-bluez.md)). The fix is kept in [`../third_party/bluez/`](../third_party/bluez/README.md), and [`patched-bluez.sh`](patched-bluez.sh) builds the package.

### Build it

Once, as root, so that apt offers `bluez`'s source and its build dependencies are installed:

1. In `/etc/apt/sources.list.d/ubuntu.sources`, make each `Types: deb` line `Types: deb deb-src`.
2. Then:
   ```sh
   apt-get update
   apt-get build-dep bluez
   apt-get install devscripts dpkg-dev fakeroot
   ```

Then, as an ordinary user, into a new folder:

```sh
tools/pi/patched-bluez.sh build ~/bluez-build
```

What it does:
- it fetches the newest `bluez` source that apt offers (`apt-get source`), or the version given after the folder;
- it adds the patches to the source's `debian/patches`, and gives the version the suffix `+local1`;
- it builds the packages with `dpkg-buildpackage -b`. On the Pi 5 that took 12 minutes (2026-11-13). Ubuntu's packaging does not run BlueZ's own tests (its `debian/rules` empties `override_dh_auto_test`), so the robot's test on the Pi is what checks the build;
- it ends with the commands that install and hold the packages this machine has installed.

### Install it and hold it

As root, with the files the build named:

```sh
apt-get install ~/bluez-build/bluez_5.85-4ubuntu0.4+local1_arm64.deb
apt-mark hold bluez
```

The package restarts `bluetooth.service` as it installs (its `postinst`, from `dh_installsystemd`). The robot's software does not register again with a BlueZ that restarted, so restart it after, when it runs with the `pi` profile.

`tools/pi/patched-bluez.sh check` then says that the installed `bluez` is the patched build of Ubuntu's newest, and held.

### Notice a new Ubuntu `bluez`

The hold keeps apt and `unattended-upgrades` from replacing the build. `unattended-upgrades` skips held packages, so Ubuntu's fixes to `bluez`, security fixes included, wait for a rebuild. Once Ubuntu publishes a new `bluez`, `tools/pi/patched-bluez.sh check` says that the installed build is not Ubuntu's newest, and exits with status 1.

`check` reads apt's lists, which the Pi refreshes daily (`APT::Periodic::Update-Package-Lists "1"`, run by `apt-daily.timer`; checked 2026-11-13). Then build again into a new folder, and install the build as above. The hold stays.

### When Ubuntu's `bluez` has the fix

The build stops with `Ubuntu's bluez <version> already has <patch>: drop it`. Then:

1. Delete the patch, and its rows in [`../third_party/bluez/README.md`](../third_party/bluez/README.md) and [`THIRD_PARTY_NOTICES.md`](../../THIRD_PARTY_NOTICES.md).
   - If none is left, delete this section and the script, and note in the dev log that ADR-0020 has ended.
2. On each Pi, as root:
   ```sh
   apt-mark unhold bluez
   apt-get install bluez
   ```
   Ubuntu's version sorts above the patched build.

### In CI

The script asks for no root and nothing of the machine but apt's sources and the build dependencies. The images of [ADR-0014](../../docs/decisions/0014-updates.md) are to be built the same way:
- on arm64, in an Ubuntu 26.04 container prepared as above;
- the image then installs the packages the build made, in place of Ubuntu's.

## The kiosk

The robot's screen is a kiosk: Ubuntu Frame (the `ubuntu-frame` snap) shows Chromium (the `chromium` snap, run as its daemon), which opens the page the robot's software serves. The screen sleeps and wakes with the robot ([ADR-0021](../../docs/decisions/0021-sleep-and-wake.md)):
- while the robot is awake, the page holds a Screen Wake Lock, and Frame keeps the screen on;
- once the robot sleeps and its face has closed its eyes, the page lets the lock go, and Frame turns the screen off 15 s later (`idle-timeout`);
- when the robot wakes, the software asks `ai-robot-wake` to light the screen. That helper presses `KEY_WAKEUP` on a keyboard of its own, which has no other key, at most once a second.

[`kiosk.sh`](kiosk.sh) sets all of this up from the files in [`kiosk/`](kiosk/):

| File | Installed as | What it does |
|---|---|---|
| `frame.config` | Frame's `config` option | no pointer; the screen off after 15 s without input or a wake lock; the idle-inhibit protocol, which the wake lock needs and Mir does not offer by default |
| `chromium.conf` | a drop-in for `snap.chromium.daemon.service` | Chromium starts with Frame and the robot, once the robot serves its page; it stops and starts with Frame, and starts again whenever it exits |
| `ai-robot.service` | `/etc/systemd/system/` | the robot's software, as its user, from this tree, with the profile given |
| `ai-robot-wake.socket` | `/etc/systemd/system/` | the helper's socket, at the robot's `[ports.display_power.helper] path`, for root and the robot's group only |
| `ai-robot-wake.service` | `/etc/systemd/system/` | the helper, as a dynamic user with the `uinput` group only, allowed no device but `/dev/uinput`; its program is copied to `/usr/local/libexec/ai-robot-wake`, where only root may change it |
| `70-ai-robot-uinput.rules` | `/etc/udev/rules.d/` | `/dev/uinput` for root and the `uinput` group |

The robot's own user leaves the `uinput` and `input` groups, if it was in them: with either, it could type into the kiosk's Chromium, which runs as root without its sandbox.

### Set it up

The robot's software first ([`software/README.md`](../../software/README.md)), and the snaps:

```sh
sudo snap install ubuntu-frame chromium
```

The profile must light the screen through the helper. The `pi` profile does; for another, such as `desktop` on a Pi without the robot's hardware, add this to `software/config/local.toml`:

```toml
[ports.display_power]
adapter = "helper"
```

Then, as the robot's user, in its tree, into a new folder:

```sh
tools/pi/kiosk.sh install pi ~/kiosk
```

It asks for no root:
- it reads the screen's port and the helper's socket from the profile's configuration;
- it writes the files above into the folder, laid out as they go on the machine (`etc/`, `usr/`), and Frame's config option beside them;
- it prints the root commands that install them, remove the robot's user from the two groups, and restart the helper, the robot, Frame and Chromium.

Run those commands as root, in their order. Changing Frame's config restarts Frame (its configure hook), and Chromium with it.

### Check it

```sh
tools/pi/kiosk.sh check pi
```

It says what differs from what `install` sets up, and exits with status 1 if anything does:
- Frame's config, as Frame's own config file holds it, and the page that Chromium shows, from its command line;
- each installed file, and the helper's copy, against this tree's;
- the groups of the robot's user, and `/dev/uinput`'s owner and mode;
- whether the units run and start at boot.

Run it again after pulling a change to `tools/pi/kiosk/` or to the helper, and `install` again if it reports one. It needs no root either.
