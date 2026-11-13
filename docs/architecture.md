# Architecture overview

> Draft — refined through the ADRs in [decisions/](decisions/README.md).

```
              ┌──────── Raspberry Pi 5 — brain: ROS 2 on Ubuntu ───────┐
 Mics ─USB──▶ │ voice: wake word, VAD, conversation                    │
 Speaker ◀USB │ vision: AI camera (IMX500)                             │ ◀─ Wi-Fi ─▶ ai-cloud
 Camera ─CSI▶ │ screen: the face on the 7" display (ui/)               │ ◀─ BLE ───▶ ai-app
 Display ◀DSI │ motion · behavior · settings · setup · cloud · system  │
              │ ros2_control: tracks, neck and arms, body sensors      │
              └───────────────────────────┬────────────────────────────┘
                                          │ body protocol (protocol/body/); the link is not chosen yet
              ┌───────────────────────────▼────────────────────────────┐
              │ Body controller — microcontroller, not chosen yet      │
              │ STS3215 servo bus ×8 · MG540 motors ×2 + encoders      │
              │ VL53L1X · NeoPixel · battery voltage · safety watchdog │
              └────────────────────────────────────────────────────────┘
```

## Responsibilities

| Block | Owns | Does not own |
|---|---|---|
| Raspberry Pi 5 | Audio, vision, face, AI, networking (built-in Wi-Fi/BLE), behavior; motion, through ros2_control's controllers | Real-time control loops |
| Body controller (microcontroller not chosen yet — [ADR-0004](decisions/0004-pi-software-first.md)) | Servos, motors and encoders, distance sensor, LEDs, battery voltage; the motors' speed loop, from the wheel speeds the Pi asks for; a safe stop when the Pi goes silent | Networking |
| [ai-cloud](https://github.com/trungnguyen278/ai-cloud) | The core AI: speech recognition, conversation, voice, and probably moods; storage, device management, updates | Physical safety: the robot checks every call locally |
| [ai-app](https://github.com/trungnguyen278/ai-app) | Set-up, settings, remote control | |

## Principles

- Pi first ([ADR-0004](decisions/0004-pi-software-first.md)): the Pi software is built before the rest, against simulated hardware.
- The Pi may hang or reboot at any time, so the body controller brings the robot to a safe state when the Pi's heartbeat is lost.
- The Pi ⇄ body protocol is defined once, in [`protocol/`](../protocol/body/README.md); so is the protocol between the Pi software and the screen ([`protocol/screen/`](../protocol/screen/README.md)).
- Software on the Pi is split into modules, each a ROS 2 node, that talk only through ROS 2 topics, services and actions, so each one can be added, replaced or removed without touching the others.
- Where heavy AI runs (camera, Pi or server) is decided in an ADR, not implicitly in code.

## Software on the Pi

Decided in [ADR-0017](decisions/0017-ros-2.md), which supersedes [ADR-0005](decisions/0005-pi-software-platform.md): ROS 2 Lyrical, the LTS supported to May 2031, on Ubuntu 26.04 LTS.

Today's code still follows ADR-0005: one Python process with its own message bus ([`software/README.md`](../software/README.md)). The [migration](#migration) below moves it.

```
nodes          system · settings · voice · vision · screen · setup · cloud · updates · behavior · motion
                 │  talk only through ROS 2 topics, services and actions (Fast DDS, on the robot only)
                 │  each node owns some ports, implemented by adapters (sim or real)
ros2_control   controllers: tracks (differential drive) · neck and arms (joint trajectories)
                 │  broadcasters: joints · distance · battery          GPIO: lights
                 │  one hardware interface ── body protocol ──▶ body controller
edges          screen ◀─ WebSocket ─▶ renderer (ui/)       cloud ◀─ WebSocket, WebRTC ─▶ ai-cloud, LiveKit
               setup ◀─ Bluetooth LE ─▶ the app
with sensors   Nav2 and SLAM Toolbox · MoveIt 2 · ROS4HRI packages: configuration, not new code
```

### From today's design to ROS 2

| Today (ADR-0005) | With ROS 2 (ADR-0017) |
|---|---|
| A module, in one process | A managed (lifecycle) node, in its own process. The launch starts the nodes in order, and restarts one that fails. |
| A message (`messages.py`) on the in-process bus | A ROS 2 interface in `ai_robot_interfaces`, or a standard one where it fits; sent on a topic, or as a service or an action |
| Checks in the message classes | Checks that the node class runs before it publishes, so a bad value still fails at the publisher |
| A port and its adapters | Unchanged, inside the node that owns the port |
| The body ports (`drive`, `joints`, `range`, `power`, `lights`, `safety`), simulated | ros2_control: the controllers and one hardware interface for the body; mock hardware or Gazebo without the body |
| `config/*.toml`: required settings, each with a comment | ROS 2 parameters: declared typed and described, with no default; their values in YAML files, each with a comment |
| Profiles (`desktop`, `pi`) and `local.toml` | Launch arguments, and `local.yaml`, git-ignored, loaded last |
| Capabilities, events and settings offered on the bus | Each node publishes its whole catalogue on a topic that keeps the last message (transient local), so a node that starts later still gets it |
| Tests on any computer with Python | Tests in the dev container: pytest for the logic, launch tests for nodes together |

### Packages

```
software/                     a colcon workspace
├── ai_robot/                 the nodes: modules, ports, adapters (Python), and their tests
├── ai_robot_interfaces/      messages, services and actions (ROS 2 interfaces)
├── ai_robot_description/     the robot's URDF, written with xacro: links, joints, limits, sensor frames
├── ai_robot_bringup/         launch files and parameters (YAML)
├── ai_robot_hardware/        the ros2_control hardware interface for the body controller (C++)
├── ai_robot_simulation/      the robot in Gazebo (gz_ros2_control) and its worlds
└── ai_robot_native/          the C++ extension for Python (pybind11), each function with its Python twin
```

### Conventions

- **Names:**
  - a node is named after its module, and its topics are `/<node>/<name>`, in lower case with underscores;
  - standard topics keep their standard names: `/joint_states`, `/tf`, `/humans/…`.
- **Quality of service**, by kind of data:
  - events, such as a tap or the start of a turn: reliable, in a short queue;
  - states, such as the screen's state or the cloud link's status: reliable, with the last one kept for nodes that start later (transient local);
  - sensor data, such as images and distances: best effort, only the newest kept;
  - audio: reliable and in order.
- **Units and frames:** SI units and radians ([REP 103](https://reps.openrobotics.org/rep-0103/)). The frames are `base_link`, `odom`, `map` and one frame per sensor ([REP 105](https://reps.openrobotics.org/rep-0105/)).
- **People** follow ROS4HRI ([REP 155](https://reps.openrobotics.org/rep-0155/)): faces and bodies from the vision node, voices from the voice node.
- **Parameters:** every value worth tuning is a parameter: declared with its type, range and description, and given in YAML with a comment. The robot's display name is set in one place.

### Security

- Discovery stays on the robot (`ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST`): nothing on the home network sees or commands the graph.
- A developer comes in through SSH. foxglove_bridge runs only on demand, through an SSH tunnel.
- The cloud and the app use their own protocols, and the cloud acts only within its safety classes.

### The edges

The screen is the exception that is real already: the `screen` module decides what it shows (the expression, a scene such as the clock or the weather, the gaze, the battery), and a renderer page in [`ui/`](../ui/README.md), shown full screen by a browser, draws it and keeps the face alive on its own. They talk over the [screen protocol](../protocol/screen/README.md) ([ADR-0006](decisions/0006-screen-renderer.md)). On the robot, Ubuntu Frame shows the page in Chromium, in kiosk mode (ADR-0017).

The `cloud` module links the robot to `ai-cloud` over the [cloud link](https://github.com/trungnguyen278/ai-platform/blob/main/protocols/cloud/README.md), which ai-platform defines.
- **Version 3.0** keeps a WebSocket open to `ai-cloud` for the session, and joins a LiveKit room only to talk, one for each conversation ([ai-platform ADR-0006](https://github.com/trungnguyen278/ai-platform/blob/main/docs/decisions/0006-two-channels.md)).
- **The robot implements both channels** (the `websocket` adapter): the session, with the cloud's calls and the robot's events, and a LiveKit room for each conversation ([ADR-0009](decisions/0009-cloud-link-webrtc.md)).
- The messages follow [ADR-0008](decisions/0008-cloud-link-messages.md).
- **Capabilities.** The cloud drives the robot through MCP tools, which the modules offer on the bus: each module offers the capabilities of what it owns, such as the screen's expressions and scenes. The robot checks every call against the capability's schema and safety class.
- **Events.** Modules declare events, and the cloud subscribes to those it wants.
- **Conversations** ([ADR-0018](decisions/0018-conversations.md)). `voice` holds them: a wake word, a touch or the cloud opens one, the cloud takes its turns, and `voice` shows them on the face, plays the answers and ends the conversation at its own limits, such as `idle`. The `cloud` module carries them, with their audio, over the link.
- **Without a server.** A simulated cloud stands in for it.

The `setup` module sets the robot up with the owner's app over Bluetooth LE, by the [set-up protocol](https://github.com/trungnguyen278/ai-platform/blob/main/protocols/setup/README.md) ([ADR-0015](decisions/0015-set-up.md)). A code on the robot's screen secures the session; the app gives the Wi-Fi network, which NetworkManager joins, and relays the robot's registration, which the `cloud` module signs with the robot's key. It may also give the robot its first languages and time zone, which the `system` module takes. The robot is bound once its server accepts it; until then it shows the set-up screen.

The `settings` module shows the robot's settings on the touch screen, as a panel the renderer draws over the face, and keeps what the owner chooses ([ADR-0016](decisions/0016-settings.md)). Each module declares its own settings on the bus and applies them, as it offers its capabilities to the cloud: the volume and the microphones are `voice`'s, the languages and the time zone are `system`'s, what the cloud may do is `cloud`'s, set-up is `setup`'s.

### Migration

Each step belongs to the final design; none of it is temporary.

1. Agree on ADR-0017 and the revised [ADR-0014](decisions/0014-updates.md). Merge the work under review: its logic, ports, adapters and protocols carry over.
2. Build the workspace in one step:
   - the interfaces for today's messages, the node class, parameters and launch;
   - the dev container and CI.

   Every module moves at once, so the robot never runs half on each bus.
3. The robot's description, and ros2_control with mock hardware and the controllers; the `motion` node on top of them.
4. The image and its releases ([ADR-0014](decisions/0014-updates.md)):
   - Ubuntu 26.04, Ubuntu Frame with Chromium, PipeWire and the camera; systemd starts it all at boot;
   - built and signed by GitHub Actions and published on GitHub Releases, with a channel file on GitHub Pages, as Home Assistant OS does;
   - the `updates` node, which installs releases through RAUC.
5. The body protocol, the body controller and its hardware interface ([ADR-0004](decisions/0004-pi-software-first.md)), once its microcontroller is chosen.
6. Nav2, SLAM Toolbox and MoveIt 2, when a range sensor and arms that can grasp are added.

The measurements on the Pi 5 (memory for each node, the face's frame rate, the camera) are taken as soon as the Pi is set up.

## Open questions (each should become an ADR)

- [x] OS, ROS 2 or not, language on the Pi, how its modules communicate — decided in [ADR-0017](decisions/0017-ros-2.md): ROS 2 Lyrical on Ubuntu 26.04, every module a node. It supersedes [ADR-0005](decisions/0005-pi-software-platform.md).
- [ ] Which microcontroller becomes the body controller ([ADR-0004](decisions/0004-pi-software-first.md)).
- [ ] Pi ⇄ body link (concept v0 shows UART/I2C) and the packet format, behind ros2_control's hardware interface ([ADR-0017](decisions/0017-ros-2.md)).
- [x] Does the body controller or the Pi drive the motor driver? Decided in [ADR-0017](decisions/0017-ros-2.md): the body controller, from the wheel speeds ros2_control asks for.
- [ ] How AI work is split between the camera, the Pi and the server. The core AI runs in the cloud; the link's [requirements](https://github.com/trungnguyen278/ai-platform/blob/main/protocols/cloud/requirements.md) are in ai-platform. The link is a WebSocket for the session, and WebRTC through LiveKit to talk ([ai-platform ADR-0006](https://github.com/trungnguyen278/ai-platform/blob/main/docs/decisions/0006-two-channels.md), version 3.0). The robot proves its identity with a key in a TPM ([ADR-0010](decisions/0010-device-identity.md)), and the messages follow [ADR-0008](decisions/0008-cloud-link-messages.md). The robot implements 3.0, its key kept in its TPM, or in a file on a development computer. Updates follow [ADR-0014](decisions/0014-updates.md); where moods live comes next.
- [x] Audio — decided in [ADR-0011](decisions/0011-audio-voice-processor.md): a USB voice processor (XMOS XVF3800) with four microphones and echo cancellation, which the robot also speaks through.
- [x] How the robot draws its screen — decided in [ADR-0006](decisions/0006-screen-renderer.md): a renderer in a full-screen browser ([ui/](../ui/README.md)), driven by the Pi software over the [screen protocol](../protocol/screen/README.md).
- [x] Set-up — decided in [ADR-0015](decisions/0015-set-up.md): the owner's app over Bluetooth LE, secured by a code on the robot's screen (SPAKE2+), which gives the Wi-Fi network and registers the robot ([set-up 2.1](https://github.com/trungnguyen278/ai-platform/blob/main/protocols/setup/README.md), in ai-platform), with its first languages and time zone. The robot's side is implemented, and so is ai-cloud's registration; the app's client is, without its screens.
