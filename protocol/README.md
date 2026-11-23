# protocol/ — what crosses a component boundary

The **single source of truth** for each protocol between the robot's components. Both sides generate their code from these definitions, or test against them; never hand-copy them (the previous generation had to keep two copies of `StateTypes` in sync by hand).

| Protocol | Between | Status |
|---|---|---|
| [Body link](body/README.md) | The Pi 5 and the body controller | Not decided yet |
| [Screen](screen/README.md) | The Pi software (`software/`) and the screen renderer (`ui/`) | Version 1 ([ADR-0006](../docs/decisions/0006-screen-renderer.md)) |
| [Cloud link](https://github.com/trungnguyen278/ai-platform/blob/main/protocols/cloud/README.md) | The robot (`software/`) and `ai-cloud` | In ai-platform, which this repository pins at `platform/`: version 3.5, whose control channel the robot implements |
| [Set-up](https://github.com/trungnguyen278/ai-platform/blob/main/protocols/setup/README.md) | The owner's app (`ai-app`), the robot and `ai-cloud`, over Bluetooth LE | In ai-platform, which this repository pins at `platform/`: version 2.0, which the robot implements |

The protocols between repositories, the cloud link and set-up, are maintained in ai-platform from 2026-10-28, and this repository pins it at `platform/`. A change to them goes to ai-platform first, and reaches the robot in the pull request that moves the pin and implements it.

A change to the robot's own protocols goes here first, then to both sides, in the same pull request ([CONTRIBUTING.md](../CONTRIBUTING.md#documentation-travels-with-the-change)).
