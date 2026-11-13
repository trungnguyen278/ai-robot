# tools/ — utility scripts

| Folder | What |
|---|---|
| [`pi/`](pi/README.md) | Setting up the robot's Raspberry Pi beyond Ubuntu's own image: a patched BlueZ ([ADR-0020](../docs/decisions/0020-patched-bluez.md)) |
| [`third_party/`](third_party/bluez/README.md) | Code from other projects that the scripts use, under its own licence ([THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)) |

Planned: ESP32-S3 flashing, AI model conversion, and test tools for individual parts.

Scripts that run on the Pi must keep LF line endings (enforced by `.gitattributes`). Check them with [ShellCheck](https://www.shellcheck.net/): `shellcheck pi/*.sh`.
