# 0020 — BlueZ: Ubuntu's package with upstream's fix, until Ubuntu ships it

- **Status:** Accepted. The patched package is the owner's choice (2026-11-13). Holding only `bluez`, and how the package is built and rebuilt, were delegated.
- **Date:** 2026-11-13

## Context

- In set-up 2.1, the robot answers `busy` to a second app while a session runs. Each app must hear only what is sent to it, so the `bluez` link asks BlueZ for a notification channel per app, through `AcquireNotify` ([#45](https://github.com/trungnguyen278/ai-robot/pull/45)).
- On 2026-11-13 the robot's Pi ran this with two centrals, under Ubuntu 26.04.1 and `bluez` 5.85-4ubuntu0.4:
  - the phone received `busy`, and nothing else;
  - when the robot then disconnected the phone, the PC lost its channel as well, mid-session. Its next request got no answer, so the robot dropped it ([dev log](../devlog/2026-11.md)).
- The cause is in BlueZ 5.85's `src/gatt-database.c`:
  - when a device that is not bonded disconnects, `clear_ccc_state` calls `ccc_write_cb` with no operation;
  - `match_client_att`, given no ATT instance, matches any channel, so BlueZ closes the first channel in its queue: the app that enabled indications first, not the one leaving;
  - that app never gets a channel again. Its CCC value has not changed, so BlueZ never asks for one.
  - Set-up's apps are not bonded: set-up secures itself with the code on the screen ([ADR-0015](0015-set-up.md)).
- Upstream fixed it in commit [`f7e2cd91`](https://git.kernel.org/pub/scm/bluetooth/bluez.git/commit/?id=f7e2cd91c2669cf50d07fe05caedf794e88f7476), "gatt-database: Fix freeing wrong client notify IO", committed 2026-10-26. BlueZ 5.88 is the first release with it; 5.86 and 5.87 lack it.
- Ubuntu's `bluez` packages, as Launchpad listed them on 2026-11-13:

| Series | `bluez` | Has the fix |
|---|---|---|
| 26.04 LTS, the robot's ([ADR-0017](0017-ros-2.md)) | 5.85-4ubuntu0.4; 5.85-4ubuntu0.5 in `-proposed` fixes only a crash in `mpris-proxy` | No |
| 26.10 | 5.87-4 | No |
| 27.04, in development | 5.88-1 | Yes |

- No Ubuntu bug asks for the fix. The next LTS is 28.04: Ubuntu releases one every two years.
- How the robot's system changes:
  - the robot's own images, of [ADR-0014](0014-updates.md), are built whole in CI. A robot never upgrades package by package;
  - the robot's Pi runs Ubuntu's own image today, upgraded in place by apt. As Ubuntu sets it up, `unattended-upgrades` installs security updates every day (checked on the Pi, 2026-11-13).

## Decision

1. **The robot runs Ubuntu's `bluez`, rebuilt with upstream's commit.** The commit is added as a Debian patch to Ubuntu's source package, under a local version suffix, `+local1`. dpkg sorts that version above the Ubuntu version it was built from, and below Ubuntu's next one. *(The owner's choice.)*
2. **One script builds it** (`tools/pi/patched-bluez.sh`):
   - it runs as an ordinary user, from Ubuntu's source package;
   - it runs on the robot's Pi today, and is written to run in CI for the images of ADR-0014;
   - the patch is kept as upstream serves it, under its own licence, in `tools/third_party/bluez/`. *(Delegated.)*
3. **On a Pi upgraded with apt, only `bluez` is held** (`apt-mark hold`). `unattended-upgrades` skips held packages and keeps installing security updates for everything else. *(Delegated.)*
4. **Each new Ubuntu `bluez` is rebuilt with the patch.**
   - `patched-bluez.sh check` says when the installed build is no longer Ubuntu's newest, or no longer held.
   - Ubuntu's fixes to `bluez`, security fixes included, reach the robot with the rebuild. *(Delegated.)*
5. **The patch is dropped once Ubuntu's `bluez` contains `f7e2cd91`.** The build stops when Ubuntu's source already has a patch. Then `bluez` is unheld and Ubuntu's own package installed.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Ask Ubuntu for a stable release update (SRU) | It takes a Launchpad bug written to the SRU template, an upload by an Ubuntu developer, the SRU team's review, and at least 7 days in `-proposed`: its timing is not the project's. Asking stays open; it would shorten this decision's life. |
| A workaround in the robot | The robot can neither reorder BlueZ's channels nor make an app enable its indications again. Its two options both break set-up: indicating through the characteristic's `Value` reaches every app that enabled indications, and refusing the second app's channel would lose `busy`. Either would build code around one BlueZ version's bug. |
| Wait for Ubuntu 28.04 | Until April 2028, a second phone near the robot during set-up would end the first phone's session. |
| Turn Ubuntu's updates off (the owner's suggestion) | It would stop the security fixes for everything else on the robot as well. Holding `bluez` alone keeps them. |
| The patched build without a hold | Its version sorts below Ubuntu's next `bluez`. Without the fix, that update would replace it unattended, and the bug would come back unnoticed. |
| Upstream's BlueZ 5.88, built from its release | It would leave Ubuntu's packaging and its patches, such as its fixes for the Raspberry Pi's Bluetooth chip and for CVEs. It would also run a BlueZ newer than the one the rest of Ubuntu 26.04 was tested with. |
| Bond the apps | Bonded devices escape the bug, but set-up would then ask people to pair their phone with the robot, a step set-up does not have ([ADR-0015](0015-set-up.md)). |

## Consequences

- Positive:
  - `busy` works as set-up 2.1 specifies, with no change to the robot's code or to the protocol;
  - the robot keeps Ubuntu's packaging, plus one upstream commit;
  - the build itself says when the patch is no longer needed.
- Negative / accepted trade-offs:
  - on a Pi upgraded with apt, Ubuntu's fixes to `bluez`, security fixes included, wait for someone to run `check` and rebuild;
  - the robot runs a `bluez` that Ubuntu did not build;
  - the machine that builds it needs `deb-src` and `bluez`'s build dependencies.
- Licence: BlueZ is GPL-2.0-or-later. An image that ships the patched build must offer its source: Ubuntu's source package and this patch. Ubuntu's mirrors drop superseded versions, so the image's build keeps the exact source package it built from.
- Follow-up:
  - the images' build in CI (ADR-0014) runs the same script, on arm64, and installs its packages;
  - whether to ask Ubuntu for an SRU, which needs a person's Launchpad account.

## References

Retrieved 2026-11-13:
- BlueZ: [commit `f7e2cd91`](https://git.kernel.org/pub/scm/bluetooth/bluez.git/commit/?id=f7e2cd91c2669cf50d07fe05caedf794e88f7476), and [5.85's `src/gatt-database.c`](https://github.com/bluez/bluez/blob/5.85/src/gatt-database.c) (`clear_ccc_state`, `match_client_att`, `ccc_write_cb`). GitHub's comparisons of tags 5.86, 5.87 and 5.88 with the commit show that only 5.88 contains it.
- Launchpad: [`bluez` in Ubuntu](https://launchpad.net/ubuntu/+source/bluez), with the [5.85-4ubuntu0.5 changes](https://launchpad.net/ubuntu/+source/bluez/5.85-4ubuntu0.5).
- Ubuntu: [the release cycle](https://ubuntu.com/about/release-cycle), and [the SRU pipeline](https://github.com/ubuntu/ubuntu-project-docs/blob/main/docs/SRU/explanation/pipeline.rst).
- unattended-upgrades 2.12, Ubuntu 26.04's version: [`is_pkg_change_allowed`](https://github.com/mvo5/unattended-upgrades/blob/2.12/unattended-upgrade) returns false for a package on hold.
- [deb-version(7)](https://manpages.debian.org/deb-version.7): how dpkg orders versions. Checked on the Pi with `dpkg --compare-versions`: `5.85-4ubuntu0.4` < `5.85-4ubuntu0.4+local1` < `5.85-4ubuntu0.5`.
