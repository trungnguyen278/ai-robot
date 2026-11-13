# software/ — robot software on the Raspberry Pi 5

> **Status:** skeleton. Every port has a simulated adapter, so the whole robot already runs on any computer; real adapters are added as the hardware arrives.
>
> **Platform:** this code follows [ADR-0005](../docs/decisions/0005-pi-software-platform.md): one Python process with its own message bus. [ADR-0017](../docs/decisions/0017-ros-2.md) supersedes it: ROS 2 Lyrical on Ubuntu 26.04, every module a node. The [migration](../docs/architecture.md#migration) moves this code there. Its modules keep their logic, ports, adapters and tests.

Python 3.11 or later (asyncio), with an optional C++ extension. Runtime dependencies:
- [aiohttp](https://docs.aiohttp.org/), for the `web` adapter of the screen, and the `websocket` adapter of the link to the cloud;
- [LiveKit's Python SDK](https://docs.livekit.io/reference/python/) (`livekit`), for the rooms of conversations, over WebRTC;
- [jsonschema](https://python-jsonschema.readthedocs.io/), to check data against the protocols' JSON Schemas;
- [cryptography](https://cryptography.io/), for the robot's key kept in software on a development computer, and set-up's encryption;
- [PyCryptodome](https://www.pycryptodome.org/), for set-up's SPAKE2+, which needs arithmetic on P-256 points that `cryptography` does not offer ([ADR-0015](../docs/decisions/0015-set-up.md));
- [dbus-fast](https://github.com/bluetooth-devices/dbus-fast), on Linux only, to reach BlueZ (Bluetooth LE) and NetworkManager (Wi-Fi) over D-Bus;
- [pymicro-wakeword](https://github.com/OHF-Voice/pymicro-wakeword), for wake words, and [pysilero-vad](https://github.com/rhasspy/pysilero-vad), for voice ([ADR-0013](../docs/decisions/0013-listening-on-the-robot.md));
- [sounddevice](https://python-sounddevice.readthedocs.io/), for the microphones and the speaker, through PortAudio: `libportaudio2` on Debian and Raspberry Pi OS;
- [tpm2-pytss](https://github.com/tpm2-software/tpm2-pytss), the `tpm` extra, for the robot's key in its TPM. It builds against the TCG software stack's libraries: `libtss2-dev` on Debian and Raspberry Pi OS.

## How it fits together

```
modules    system · settings · motion · voice · vision · screen · setup · cloud · behavior
             │  talk to each other only through messages on the bus
             │  each one owns some ports
ports      audio_in · audio_out · wake_word · voice_activity · camera · screen
           setup_link · network · cloud · identity · binding · settings · daylight · time_zone
           drive · joints · range · power · lights · safety
             │  implemented by
adapters   sim (any computer, CI)  |  real (on the robot), chosen per profile
```

| Module | Owns ports | Responsibility (planned) |
|---|---|---|
| `system` | `power`, `lights`, `time_zone` | Battery reports, with whether the battery is low; status light; the charging scene when plugged in; the robot's languages, a setting, and its time zone, the system's, both of which set-up can give; planned: clean shutdown on low battery |
| `motion` | `drive`, `joints`, `range`, `safety` | Tracks, neck and arms; obstacle and emergency-stop checks |
| `voice` | `audio_in`, `audio_out`, `wake_word`, `voice_activity` | Conversations with the cloud: the wake word opens them, the robot streams what it hears and plays the answers, barge-in, the limits that end them; where the conversation is (the interaction state). The voice processor cancels the echo ([ADR-0011](../docs/decisions/0011-audio-voice-processor.md), [ADR-0013](../docs/decisions/0013-listening-on-the-robot.md), [ADR-0018](../docs/decisions/0018-conversations.md)) |
| `vision` | `camera` | People and object detection, face tracking |
| `screen` | `screen` | What the screen shows: the expression and the scene asked with the highest priority, the gaze, the voice, the battery and whether the microphones are off; panels over the face, such as the settings; taps and holds |
| `settings` | `settings` | The owner's settings: what the modules offer, shown as one panel on the screen; what the owner chose, kept, and handed to their modules before they run ([ADR-0016](../docs/decisions/0016-settings.md)) |
| `setup` | `setup_link`, `network` | Set-up with the owner's app over Bluetooth LE: when it opens, the code on the screen, the session, the Wi-Fi network ([set-up](https://github.com/trungnguyen278/ai-platform/blob/main/protocols/setup/README.md), in ai-platform) |
| `cloud` | `cloud`, `identity`, `binding` | The link to `ai-cloud`, which the robot's identity proves: the cloud calls the capabilities the modules offer, and hears the events they declare; it tells the cloud the robot's languages and time zone, which `system` gives, and carries conversations, turns and answers with their audio, for `voice` ([the cloud link](https://github.com/trungnguyen278/ai-platform/blob/main/protocols/cloud/README.md), in ai-platform). It signs the robot's registration at set-up, and keeps whether the robot is bound. Its settings say what the cloud may do, by safety class |
| `behavior` | `daylight` | Decides what the robot does: for now, reactions to taps, the clock when nobody interacts (taps and conversations count), a sleepy face from sunset to sunrise |

- **Module:** one function of the robot (`core/module.py`). It owns some ports and talks to the other modules only through the bus.
- **Message:** what a module tells the others; a frozen dataclass in `messages.py`, whose class is the topic.
- **Port:** the interface to one piece of hardware or one service (`ports/`).
- **Adapter:** an implementation of a port, simulated or real (`adapters/`).
- **Profile:** which adapter each port uses, and its settings (`config/profiles/`).

Lifecycle: open every adapter → `start()` each module in the configured order → run all the modules' `run()` together → on Ctrl+C, SIGTERM or a failing module (also during startup), `stop()` the modules in reverse order → close the adapters. Each shutdown step gets at most `[app] shutdown_step_timeout` seconds; a second Ctrl+C forces the exit.

## Layout

```
software/
├── pyproject.toml          Package, development tools, pytest and ruff settings
├── config/
│   ├── base.toml           Settings shared by every profile, including the robot's display name
│   ├── profiles/           desktop.toml (everything simulated), pi.toml (Raspberry Pi 5)
│   └── local.example.toml  Template for local.toml: settings for one machine (git-ignored)
├── src/ai_robot/
│   ├── __main__.py         Command line (ai-robot)
│   ├── app.py              Builds the robot from the configuration and runs it
│   ├── messages.py         Messages between modules
│   ├── native.py           C++ functions, with pure-Python fallbacks
│   ├── core/               Configuration, bus, module base class, factory
│   ├── ports/              Port interfaces
│   ├── adapters/           Adapter registry; sim/ (simulated), portaudio/ (microphones and speaker), microwakeword/ (wake words), silero/ (voice), web/ (aiohttp: the screen served to a browser), websocket/ (the link to the cloud), keys/ (keys kept in software), tpm/ (keys in the robot's TPM), files/ (state kept in files), bluez/ (Bluetooth LE), networkmanager/ (Wi-Fi), sun/ (sunrise and sunset)
│   └── modules/            Module registry and the modules
├── native/                 Optional C++ extension (pybind11 + scikit-build-core)
└── tests/
```

## Run and test

On any computer with Python 3.11 or later (Raspberry Pi OS "Trixie" ships Python 3.13 and Ubuntu 26.04 ships 3.14, as of October 2026):

```sh
cd software
python3 -m venv .venv
. .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

ai-robot --profile desktop        # Ctrl+C to stop
pytest
ruff check . && ruff format --check .
```

On the Raspberry Pi, use `--profile pi` or set `AI_ROBOT_PROFILE=pi`. It keeps the robot's identity in its TPM, and runs the simulated adapters for the ports without a real one yet. It needs:
- the TCG software stack's libraries and the `tpm` extra: `sudo apt install libtss2-dev`, then `pip install -e ".[dev,tpm]"`;
- the TPM's overlay in `/boot/firmware/config.txt`: `dtoverlay=tpm-slb9670` ([TPM notes](../hardware/components/letstrust-tpm.md));
- PortAudio, for the microphones and the speaker: `sudo apt install libportaudio2`;
- the voice processor as PipeWire's default input and output, so that everything the robot plays reaches its echo canceller: find its two devices with `wpctl status`, then `wpctl set-default <id>` for each;
- access to `/dev/tpmrm0` for the user who runs the robot, through the `tss` group: `sudo usermod -aG tss "$USER"`, then log in again;
- for set-up, the Wi-Fi through NetworkManager, and the time zone through systemd-timedated. NetworkManager's polkit policy lets a user in a desktop session scan and join networks, and Debian's own rule lets the `netdev` and `sudo` groups save them; timedated asks an administrator before it sets the time zone (`org.freedesktop.timedate1.set-timezone`). A robot run from SSH, or as a service, needs a rule of its own for the user who runs it, which this writes:

  ```sh
  sudo tee /etc/polkit-1/rules.d/50-ai-robot.rules > /dev/null <<EOF
  polkit.addRule(function (action, subject) {
      if (subject.user == "$USER" && (
              action.id == "org.freedesktop.NetworkManager.network-control" ||
              action.id == "org.freedesktop.NetworkManager.wifi.scan" ||
              action.id == "org.freedesktop.NetworkManager.settings.modify.system" ||
              action.id == "org.freedesktop.timedate1.set-timezone")) {
          return polkit.Result.YES;
      }
  });
  EOF
  ```

  BlueZ's D-Bus policy lets any user offer the set-up service. These are the policies of Debian Trixie's `network-manager` 1.52.1 and `bluez` 5.82 packages, and of Ubuntu 26.04's 1.54.3 and 5.85 (checked 2026-10-27), and systemd's own for timedated (checked 2026-10-28); the robot's image will carry its own rule ([ADR-0014](../docs/decisions/0014-updates.md)).

On a Pi without its TPM, select the `file` identity in `config/local.toml` (see `local.example.toml`).

`ai-robot --help` lists the options; `--duration SECONDS` stops the robot after a while, which is handy for quick checks. The environment variables are listed in [`.env.example`](.env.example).

Install with `-e` from this checkout, cloned with its submodule (`git clone --recurse-submodules`, or `git submodule update --init` after a plain clone): the configuration is then read from `software/config`, the robot's own protocols from `protocol/`, and those it shares with the cloud and the app from ai-platform, pinned at `platform/`. With any other kind of install, point:
- `--config-dir` or `AI_ROBOT_CONFIG_DIR` at a configuration directory;
- `AI_ROBOT_PROTOCOL_DIR` at the `protocol` folder;
- `AI_ROBOT_PLATFORM_DIR` at ai-platform's `protocols` folder;
- the screen's `ui_dir` setting at the `ui` folder.

### The screen

While the robot runs, its screen is at <http://127.0.0.1:8770/>: open it in a browser to watch the face follow the robot. On the robot, Chromium shows that page full screen (kiosk mode). The page is the renderer in [`ui/`](../ui/README.md); the `screen` module decides what it shows, over the [screen protocol](../protocol/screen/README.md).

### Voice

The `voice` module holds the robot's conversations ([ADR-0013](../docs/decisions/0013-listening-on-the-robot.md), [ADR-0018](../docs/decisions/0018-conversations.md)):
- **Opening.** The wake word opens a conversation, when the cloud takes them; without the cloud, or when the conversation cannot start, the face shows that the robot cannot talk. The cloud may open one too, to listen or only to talk; a wake word ends one that does not listen, and opens another.
- **Listening.** The robot streams what it hears for the whole conversation. The cloud takes the turns, and the face shows them: listening, thinking, speaking, and attentive between them.
- **Barge-in:** voice over an answer (`barge_in`) stops it at once, and tells the cloud, which already has the words. Without a voice processor that cancels the echo (`echo_cancelled` of `[ports.audio_in]`), the robot streams nothing while it speaks, and no voice interrupts it.
- **Limits.** The robot ends a conversation when nobody has spoken since it opened (`nothing_heard`), after a silence of `idle` seconds, after `longest` seconds, or when its microphones go off. Speech that the cloud drops as not meant for the robot counts as silence.

Its settings are the volume, the microphones (off, the robot hears nothing, not even its wake word, and the screen shows it), and the silence that ends a conversation, which trades privacy for ease.

On a computer the simulated adapters hear silence, so no conversation opens. To talk to the robot on a computer with a microphone, select `portaudio` for `audio_in` and `audio_out`, `micro` for `wake_word` and `silero` for `voice_activity` in `config/local.toml`; without a voice processor, its own voice is not cancelled, so use headphones. The wake word is a model of `[ports.wake_word.micro] models`; the robot's own is trained for its name, once the name is chosen.

### The cloud

By default the robot talks to a simulated cloud (the `sim` adapter of the `cloud` port). It opens a session once the robot has said what it is, and asks for nothing, so the robot runs without a server.

The real link is the `websocket` adapter: the control channel of the [cloud link](https://github.com/trungnguyen278/ai-platform/blob/main/protocols/cloud/README.md) 3.0, a WebSocket to `ai-cloud` that stays open ([ai-platform ADR-0006](https://github.com/trungnguyen278/ai-platform/blob/main/docs/decisions/0006-two-channels.md)). Before each connection, the robot asks `ai-cloud` for an access token, and proves who it is with its `identity` port: an identifier, and a key that signs for it ([ADR-0010](../docs/decisions/0010-device-identity.md)).
- **Its key.** On the robot, the `tpm` adapter keeps it in the robot's TPM, which makes the key and never lets it out. A factory reset clears the TPM, which gives the robot a new identity, to be set up again. On a development computer, the `file` adapter keeps it in a file of your choosing, made on first use and readable only by you; like SSH, it refuses a key file that others can open. The `sim` adapter, the default, makes a new key at each start, which no server knows.
- **Its session** outlives a lost connection. The robot numbers what it sends and keeps it until the cloud has it, pings at the pace the cloud sets, and connects again after a random wait that grows with each failure, resuming its session. If the cloud no longer speaks its version, the robot logs that it needs an update, and tries again only now and then.
- **Conversations** are asked for and ended on the session, and held in a LiveKit room of their own, which the cloud names ([ADR-0009](../docs/decisions/0009-cloud-link-webrtc.md), [ADR-0018](../docs/decisions/0018-conversations.md)). Only agents may hear the robot's microphone, and they hear first what it heard from its wake word on, as LiveKit's pre-connect audio. The robot plays only agents' voices, and takes the turns and answers only from agents. It refuses a conversation the cloud opens to listen while its owner does not allow the cloud to listen, or its microphones are off.
- **A server.** `ai-cloud` has no code yet: the link runs against the fake cloud of the tests (`tests/fake_cloud.py`).

The server is the one in `server` until the robot is set up with another: set-up registers the robot with the server the owner's app names, and the robot then uses that one.

The cloud sees only the capabilities whose safety class is listed in `[modules.cloud] allowed_classes`, a setting the cloud cannot change. The tests of conversations' rooms run against a real LiveKit server when one is found: the path to `livekit-server` in `LIVEKIT_SERVER`, or the program on the `PATH`. The tests of the `tpm` adapter run against a software TPM, with the `tpm` extra installed: the path to `swtpm` in `SWTPM`, or the program on the `PATH` (`sudo apt install swtpm`).

### Set-up

The `setup` module sets the robot up with the owner's app, over Bluetooth LE ([set-up](https://github.com/trungnguyen278/ai-platform/blob/main/protocols/setup/README.md), in ai-platform; [ADR-0015](../docs/decisions/0015-set-up.md)):
- **When.** Set-up opens by itself while the robot is not bound, or when someone at the robot asks; it shows a code and its QR code on the screen, and closes after `window` seconds, when it is done, or after `attempts` wrong codes. A tap opens it again while the robot is not bound.
- **The session.** The code secures it, through SPAKE2+ (`core/spake2plus.py`, checked against RFC 9383's test vectors), then AES-GCM (`core/setup_session.py`). The app scans for networks, joins one, and asks for the robot's registration, which the `cloud` module signs with the robot's key.
- **One app at a time.** The session belongs to the first app whose `hello` comes. Another app's `hello` gets `busy`, sent to that app only, and the robot drops it. Messages are read as JSON that every side can hold (`core/data.py`'s `read_json`, [ai-platform ADR-0014](https://github.com/trungnguyen278/ai-platform/blob/main/docs/decisions/0014-json-every-side-can-hold.md)).
- **Bound.** The robot is bound once the server it was registered with accepts it; the `binding` port keeps that, so that the robot knows it while offline (a file on the robot).

On a computer, the `sim` adapters stand in: no app connects, and the simulated binding says that the robot is bound. The tests play the app through the simulated link (`tests/test_setup.py`). The `bluez` and `networkmanager` adapters are tested against fake BlueZ and NetworkManager services on a private D-Bus (`tests/test_bluez_setup.py`, `tests/test_networkmanager.py`), which needs Linux and `dbus-daemon` (`sudo apt install dbus-daemon`). The fake BlueZ takes each app's channel for indications as BlueZ does, a socket passed over D-Bus by `AcquireNotify`. The `bluez` link ran a whole session on the robot's Pi, with a Windows PC as the app, when it still indicated through the characteristic's `Value` (2026-10-28, [dev log](../docs/devlog/2026-10.md)). Through `AcquireNotify`, it ran on the Pi with a PC and a phone on 2026-11-13, a second app turned away with `busy` included ([dev log](../docs/devlog/2026-11.md)). That needs a BlueZ with upstream's commit `f7e2cd91`, first in 5.88: on Ubuntu 26.04, the patched build of [ADR-0020](../docs/decisions/0020-patched-bluez.md). With Ubuntu's own 5.85, the first app's session ends when the robot drops the second app.

### Settings

The owner changes the robot's settings on its screen. A press held still on the face opens them, and so can the cloud (`settings.open`), so that "open the settings" works by voice ([ADR-0016](../docs/decisions/0016-settings.md)):
- **Declared by their modules.** Each module offers its own settings on the bus, as it offers capabilities, and applies them. The `settings` module shows them all, as one panel the screen draws.
- **Kept.** The `settings` port keeps what the owner chose: a file on the robot (the `file` adapter), nothing on a computer (`sim`). At start, the `settings` module hands each module its kept values before the modules run, so that a choice holds from the first moment, such as what the cloud may not do.
- **Changed.** A change shows at once. The module that owns the setting applies it or refuses it; a refused value goes back.

The settings so far: set-up with the app and the Wi-Fi network (`setup`), the volume and the microphones (`voice`), what the cloud may do, by safety class (`cloud`), and the robot's name, software, account and identifier.

### C++ extension

```sh
pip install ./native              # needs a C++17 compiler; pip fetches CMake, pybind11 and scikit-build-core if needed
```

Run it again after changing the C++ code. `ai_robot.native.AVAILABLE` tells whether the extension is in use; without it, the pure-Python fallbacks give the same results (`tests/test_native.py`).

## Configuration

Files are merged in order: `config/base.toml`, then `config/profiles/<profile>.toml`, then `config/local.toml` if present. Tables merge key by key; any other value, lists included, is replaced. `--config-dir` or `AI_ROBOT_CONFIG_DIR` selects another configuration directory.

```toml
[ports.camera]
adapter = "sim"          # the adapter that implements this port
fps = 15.0               # passed to every adapter of this port

[ports.screen.web]       # passed only to the "web" adapter
port = 8770

[modules.system]
battery_interval = 5.0   # passed to the module
```

Settings become keyword arguments of the adapter's or module's constructor, so a misspelt or missing key, or a value of the wrong type, stops the robot at startup with a message that names the table. Secrets such as API keys never go into these files: read them from environment variables.

**Every value worth tuning is in `config/base.toml`**, with a comment: timings, rates, limits, thresholds, colours, hardware values, and what the simulated hardware reports. The code takes each one as a required setting, with no default, so the file shows the values the robot really uses, and a test builds every module and every adapter from it (`tests/test_registry.py`). To change one value on one machine, copy its key into `local.toml`, which git ignores; `local.example.toml` shows how.

What stays in the code, named once at the top of its file:
- values a protocol fixes, with a pointer to its spec in `protocol/`;
- facts, with their source;
- details nobody needs to tune, such as how many message ids the cloud link remembers.

## Extending

- **A module:** create `src/ai_robot/modules/<name>.py` with a `Module` subclass that lists its ports in `owns`; register it in `MODULES` (`modules/__init__.py`); add it to `[modules] enabled` in `config/base.toml`; add tests. When the file grows, turn it into a package (`modules/<name>/__init__.py` importing the class); the registry entry stays the same.
- **A message:** add a frozen dataclass that derives from `Message` to `messages.py`.
- **A port:** add an abstract class that derives from `Port` under `ports/` and register it in `PORT_TYPES`; add a simulated adapter; give the port to one module; configure it in `config/base.toml`.
- **A capability or an event for the cloud:** do it in the module that owns what it touches.
  1. Create a `CapabilityHost` (`core/capabilities.py`) in `__init__`.
  2. `add` each `Capability` with its handler. A capability has a name that starts with the module's name, JSON Schemas for its arguments and its result, and a safety class.
  3. `add_event` each `EventKind`.
  4. `offer()` them in `start`.
  5. Run `serve()` in `run`, and `emit()` the events.

  The cloud module passes them on, and the protocol does not change.
- **A setting the owner can change:** also in the module that owns what it changes.
  1. Create a `SettingsHost` (`core/settings.py`) in `__init__`.
  2. `add` each `Setting` with its handler. A value has a JSON Schema: a boolean shows as a switch, a number between two bounds as a slider, one of several constants, each with a title, as a choice. An action shows as a button, an information as a line.
  3. `offer()` them in `start`.
  4. In `run`, `await apply_pending()` before anything uses the values, then run `serve()`.

  A handler that cannot apply a value raises `ValueError`, and the panel puts the value back.
- **An adapter:** implement the port in `adapters/<technology>/` (for example `adapters/alsa/`), register it in `ADAPTERS` (`adapters/__init__.py`) and select it in a profile. Import its libraries only inside that folder, so that the other profiles keep working without them.

## Rules

- Each port has exactly one owner; modules talk only through the bus; hardware and services are reached only through ports.
- Never block the event loop: run blocking library calls with `asyncio.to_thread`, and heavy computation in C++ or in another process.
- No hard-coded values: anything worth tuning lives only in the configuration, and the code takes it as a required setting, with no default (see [Configuration](#configuration)). The full rule is in [AGENTS.md](../AGENTS.md).
- Motion commands expire (`ttl`, at most `max_ttl`), so a hung caller can never leave the robot moving; `motion` is the first module to stop.
- Every C++ function has a pure-Python fallback with the same behavior, and a test that compares them.
- The robot's display name lives only in `config/base.toml` ([ADR-0003](../docs/decisions/0003-neutral-repo-naming.md)).
