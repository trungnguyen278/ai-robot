# Architecture decision records (ADRs)

Each significant decision is a short file: context, decision, alternatives considered and consequences — so that years from now we still know **why**.

- Template: [0000-template.md](0000-template.md). How to write one: the `adr` checklist (`.claude/skills/adr/SKILL.md`).
- Never edit an old ADR to change a decision; write a new one and mark the old one "Superseded by …".
- From 2026-10-28, a decision that spans the project's repositories (a protocol between them, identity, languages) is recorded in [ai-platform](https://github.com/trungnguyen278/ai-platform/tree/main/docs/decisions) ([ai-platform ADR-0001](https://github.com/trungnguyen278/ai-platform/blob/main/docs/decisions/0001-repositories.md)); the robot's own decisions stay here.

| # | Decision | Status | Date |
|---|---|---|---|
| [0001](0001-raspberry-pi-5-8gb.md) | Raspberry Pi 5 (8 GB) as the robot's brain | Accepted | 2026-10-15 |
| [0002](0002-esp32-s3-body-controller.md) | ESP32-S3 as body controller for real-time I/O only | Superseded by [0004](0004-pi-software-first.md) | 2026-10-15 |
| [0003](0003-neutral-repo-naming.md) | Descriptive repository names without the product name | Superseded by [ai-platform ADR-0001](https://github.com/trungnguyen278/ai-platform/blob/main/docs/decisions/0001-repositories.md) | 2026-10-15 |
| [0004](0004-pi-software-first.md) | Raspberry Pi software first; body controller not chosen yet | Accepted | 2026-10-15 |
| [0005](0005-pi-software-platform.md) | Pi software platform: Raspberry Pi OS, Python with C++ extensions, no ROS 2 for now | Superseded by [0017](0017-ros-2.md) | 2026-10-15 |
| [0006](0006-screen-renderer.md) | Screen: a browser renderer with a procedural face, driven by the Pi software | Accepted | 2026-10-16 |
| [0007](0007-cloud-link-transport.md) | Cloud link transport: one WebSocket over TLS, opened by the robot, with the audio kept separable | Superseded by [0009](0009-cloud-link-webrtc.md) | 2026-10-16 |
| [0008](0008-cloud-link-messages.md) | Cloud link messages: typed JSON, MCP for capabilities, binary audio frames | Accepted; the audio frames superseded by [0009](0009-cloud-link-webrtc.md); amended by ai-platform [ADR-0001](https://github.com/trungnguyen278/ai-platform/blob/main/docs/decisions/0001-repositories.md), [ADR-0003](https://github.com/trungnguyen278/ai-platform/blob/main/docs/decisions/0003-compatibility.md) and [ADR-0006](https://github.com/trungnguyen278/ai-platform/blob/main/docs/decisions/0006-two-channels.md) | 2026-10-16 |
| [0009](0009-cloud-link-webrtc.md) | Cloud link transport: WebRTC through a LiveKit server | Accepted; amended by [ai-platform ADR-0006](https://github.com/trungnguyen278/ai-platform/blob/main/docs/decisions/0006-two-channels.md) | 2026-10-22 |
| [0010](0010-device-identity.md) | Device identity: a key in a TPM, short-lived tokens from signed assertions | Accepted | 2026-10-22 |
| [0011](0011-audio-voice-processor.md) | Audio: a USB voice processor (XMOS XVF3800) that the robot also speaks through | Accepted | 2026-10-22 |
| [0012](0012-end-of-speech.md) | The end of the user's speech: the cloud decides, the robot falls back | Accepted; amended by [ai-platform ADR-0006](https://github.com/trungnguyen278/ai-platform/blob/main/docs/decisions/0006-two-channels.md) and [0018](0018-conversations.md) | 2026-10-23 |
| [0013](0013-listening-on-the-robot.md) | Listening on the robot: microWakeWord, Silero VAD, and PipeWire | Accepted; amended by [0018](0018-conversations.md) | 2026-10-23 |
| [0014](0014-updates.md) | Updates: whole signed images in two slots, with RAUC | Accepted; amended by [ai-platform ADR-0001](https://github.com/trungnguyen278/ai-platform/blob/main/docs/decisions/0001-repositories.md) | 2026-10-24 |
| [0015](0015-set-up.md) | Set-up: the app over Bluetooth LE, secured by a code on the robot's screen | Accepted; amended by ai-platform [ADR-0001](https://github.com/trungnguyen278/ai-platform/blob/main/docs/decisions/0001-repositories.md) and [ADR-0005](https://github.com/trungnguyen278/ai-platform/blob/main/docs/decisions/0005-homes.md) | 2026-10-24 |
| [0016](0016-settings.md) | Settings: declared by the modules, shown as a panel on the touch screen | Accepted | 2026-10-25 |
| [0017](0017-ros-2.md) | ROS 2 on the robot: every module a node, the body through ros2_control, on Ubuntu | Accepted | 2026-10-27 |
| [0018](0018-conversations.md) | Conversations on the robot: one wake word, many turns | Accepted | 2026-10-28 |
| [0019](0019-setup-button.md) | Set-up without the touch screen: hold the microphones' mute button | Accepted | 2026-10-29 |
| [0020](0020-patched-bluez.md) | BlueZ: Ubuntu's package with upstream's fix, until Ubuntu ships it | Accepted | 2026-11-13 |
| [0021](0021-sleep-and-wake.md) | Sleep and wake: the robot decides, the kiosk turns the screen off and on | Accepted | 2026-11-14 |
