# 0023 — The factory reset: on the robot's screen, leaving its home first, erased by its image

- **Status:** Accepted.
  - The owner chose the reset's direction on 2026-11-14: where it starts, how it is confirmed, leaving the home first, erasing offline, a new identity afterwards, and the button later.
  - The rest was delegated: the port and its adapters, the order of the steps, the screen's words, and how the robot starts again.
- **Date:** 2026-11-19

## Context

- **What it must do.** The owner must be able to erase the robot simply (ETSI EN 303 645, provision 5.11-1), and a robot passes to a new owner only through such a reset (PROV-4, [ADR-0022](0022-a-removed-robot-keeps-its-data.md)).
- **The image.** A factory reset wipes the data partition and clears the TPM ([ADR-0014](0014-updates.md), decision 5), which holds the robot's identity ([ADR-0010](0010-device-identity.md)).
- **The cloud.** [ai-platform ADR-0015](https://github.com/trungnguyen278/ai-platform/blob/main/docs/decisions/0015-factory-reset.md) sets the cross-repository part:
  - the reset starts on the device;
  - online, the device first leaves its home with the cloud link 3.2's `leave`, keeping that it is resetting before it sends it;
  - offline, it erases anyway after `leave_timeout`, and its screen says to remove it in the app;
  - afterwards it is a new device.
- **systemd** wants a factory reset to "take place during early boot", with the request carried to the next boot ([systemd.io/FACTORY_RESET](https://systemd.io/FACTORY_RESET/), read 2026-11-13 and 2026-11-19):
  - an interface that offers a reset "should first check if requesting a factory reset is supported at all via the Varlink service", then "ask for activation of the `factory-reset.target` unit";
  - its own request is an EFI variable. "On non-EFI systems, a different mechanism should be devised": a service "plugged into `factory-reset.target`", and `systemd.factory_reset=1` on the next boot's kernel command line.
  - The Varlink service's `CanRequestFactoryReset` answers only whether the system booted with EFI (`is_efi_boot()`, v259's `factory-reset-tool.c`). Its `GetFactoryResetMode` reports `unsupported`, `unspecified`, `off`, `on` (this boot is a reset's), `complete` (the reset is done) or `pending` (asked for the next boot) ([systemd-factory-reset(8)](https://www.freedesktop.org/software/systemd/man/latest/systemd-factory-reset.html), and v259's `src/shared/factory-reset.c`, read 2026-11-19).
- **Ubuntu 26.04 on the Pi** has systemd 259.9-0ubuntu0.1 (read on the robot's Pi, 2026-11-19):
  - its `factory-reset.target` wants the request (`ConditionFirmware=uefi`), `systemd-factory-reset-reboot.service`, which restarts the system, and the PCR phase. It does not want `systemd-tpm2-clear.service`.
  - The Pi boots without EFI: the mode is `unspecified`, and `CanRequestFactoryReset` is false. Starting the target restarts the Pi with no request, and its job is still `done`. A robot that started it at each start would restart for ever.
- **The TPM.** systemd's TPM clear goes through the firmware's Physical Presence Interface, which needs ACPI. A Pi 5 booted from a device tree has none. `TPM2_Clear` from the system "requires Platform Authorization or Lockout Authorization" (TCG TPM 2.0 Library, Part 3, r1.59, 24.6).
- **Where products put it** (read 2026-11-13):
  - Echo Show: Settings > Device Options > Reset to Factory Defaults;
  - Amazon Astro: Settings > Device Options > Reset to Factory Defaults;
  - Meta Portal: Settings > General > Factory Reset.
  - Each also has a button held for a while when the screen does not work: the Echo for about 15 s.

## Decision

1. **Settings > About > Reset to factory settings,** the last item of About. The settings open with a finger held on the face ([ADR-0016](0016-settings.md)).
2. **Only the owner starts a reset,** on the robot's own screen:
   - the settings never send an action again from what they kept (`SettingPressed`, [ADR-0016](0016-settings.md)), whatever the settings file holds;
   - the screen's WebSocket refuses a page from another site, by its `Origin`, as Tornado's `check_origin` does (v6.5.2, read 2026-11-19). A client with no `Origin` is no browser's (RFC 6455, 4.1), and is let in.
3. **A second screen** asks "Reset to factory settings?". It says what goes: the Wi-Fi networks, the name and the settings, and that the robot leaves the home. It says that the robot starts again as a new one, and that "This cannot be undone." Its answers are Cancel and Erase. There is no PIN: the owner is beside the robot.
   - The screen protocol's panel items gain two optional fields for this, `confirm_lines` and `confirm_label`. Its version stays 1.
4. **The steps, in this order** (`modules/reset.py`):
   1. the robot keeps that a reset began (`factory_reset.begin`). The record is on the disk, its folder synced, before the robot leaves (fsync(2), Linux man-pages 6.19);
   2. `Resetting` closes set-up, ending any session in it, and the settings, which both stay closed. The cloud module stops keeping the binding;
   3. the robot leaves its home (`cloud`, `leave`). A robot never set up is in no home, and leaves at once;
   4. if it could not leave, its screen says "Remove me in the app" for `note` seconds, 10 as shipped;
   5. it erases itself (`factory_reset.erase`) and starts again, in set-up, as a new robot.
   - The screen shows each step: the `reset` scene, which only the robot can ask for.
   - The reset module needs the `cloud` module, which answers whether the robot left (`Module.needs`): the robot does not start with the one and without the other.
5. **A reset cut short goes on at the next start.** If a reset began, the reset module says so before any module runs, so set-up never opens. Then it goes on from step 3.
   - The record is the robot's own: `{"resetting": true}`, to which the `systemd` adapter adds `boot`. A file that holds anything else is logged by its path, and starts nothing.
6. **The reset is a port, `factory_reset`, with an adapter for each kind of robot:**
   - **`systemd`, the robot's image** (the `pi` profile):
     - it keeps its record on the data partition;
     - first, it reads the mode from systemd's Varlink service. `unsupported` is refused, and so are `on` and `complete`: this boot is a reset's, and the record outlived it. `unspecified`, `off` and `pending` go on;
     - it keeps in its record the boot it asks in (`/proc/sys/kernel/random/boot_id`, [sd_id128_get_boot(3)](https://www.freedesktop.org/software/systemd/man/latest/sd_id128_get_machine.html)). If the record names an earlier boot, the system restarted without erasing: the robot stops with an error instead of asking again;
     - over D-Bus, it asks systemd to start `factory-reset.target`, and waits for the job's result. Only `done` counts.
     - The image's units then carry the request to the next boot and restart the system. In early boot, the system wipes the data partition (systemd-repart's `FactoryReset=`), which takes the record with it, and clears the TPM.
     - Until then, a robot started again goes on with the reset.
   - **`files`, a development computer, or a Pi that runs the robot from its repository:**
     - it erases the files and folders its configuration lists: the state folder, and a `file` identity's key file. Only the robot's own may be listed: what is in a folder named as the record's, such as `~/.local/state/ai-robot` and `~/.config/ai-robot` (XDG Base Directory Specification 0.8), and never the home folder or one that holds it;
     - it leaves the system's time zone. It is the computer's, which timedated sets for all its users, and set-up sets it again from the new owner's phone;
     - it forgets the Wi-Fi profiles that set-up made. Set-up marks each profile it makes in NetworkManager's `user.data`, as `ai-robot.setup` = `joined`;
     - it deletes its record last, so that an erase cut short starts again;
     - then the robot starts again in its own process.
   - **`sim`**, for the tests and the desktop: it erases nothing.
7. **The robot starts again in its process** (`RestartRequested`). The app stops, and a new one is built from the configuration and the files as they now are. This works the same with or without a service manager. On the image, the system restarts it.
8. **Not now:** a button held for about 15 s, as on the Echo, once the body's buttons are chosen.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Start the reset from the app, as aibo does | The owner chose the robot's screen. A reset from the app needs the cloud, so it cannot erase an offline robot (ETSI 5.11-1). |
| Hold-to-confirm, or a PIN | Neither the Echo nor the Astro asks for one, and the owner is beside the robot. One extra screen with an explicit Erase is enough. |
| On the image, erase from the robot's process: delete the data and run `tpm2_clear` | The robot's user would need write access to everything on the data partition, and the TPM's lockout authorization. systemd erases in early boot, with nothing running. |
| Evict only the robot's own TPM keys | The owner chose a new identity. ADR-0010 and ADR-0014 clear the TPM. |
| Exit with a code, and let systemd restart the robot | A development computer has no service manager. A restart in the process is the same everywhere, and tests can see it. |
| Forget every Wi-Fi profile | Some profiles are not the robot's, such as the Ethernet one and those made with `nmcli`. Only the profiles set-up made are forgotten. |
| Wait for the cloud before erasing | An offline robot could never be erased (ETSI 5.11-1). |
| Ask only when `CanRequestFactoryReset` is true | It is true only on EFI. The Pi boots without, and its image plugs its own request into the target, which systemd cannot report. The boot kept in the record stops a restart that erased nothing, whatever the request. |
| Erase any path the configuration lists | One mistake, such as `~`, would erase the owner's home. |
| The `files` adapter sets the time zone back | To what? The computer's own was chosen by its user before the robot ran, and set-up sets it again. |

## Consequences

- Positive:
  - One path erases the robot, from its own screen, and it works offline.
  - Each adapter belongs to the final design.
  - A power cut during a reset does not leave the previous owner's data behind.
- Negative / accepted trade-offs:
  - A robot reset offline stays in its home's list, not connected, until someone removes it. Its screen says so first.
  - The `files` adapter does not forget Wi-Fi profiles made before set-up marked them. These exist only on development robots, since the image starts with none.
  - If an erase fails, the robot stops with the error and tries again at its next start.
- Follow-up, for the image ([ADR-0014](0014-updates.md)):
  - plug a request into `factory-reset.target`, as systemd.io says of systems without EFI: a service that puts `systemd.factory_reset=1` on the Pi's next kernel command line, and removes it once the reset is done. Ubuntu's own request runs only on EFI;
  - run systemd-repart with `FactoryReset=yes` on the data partition, in early boot;
  - clear the TPM with `TPM2_Clear`, with an authorization that is not kept on the data partition. Ubuntu's target does not want `systemd-tpm2-clear.service`, which needs ACPI anyway;
  - set the time zone back to the image's, unless it is on the data partition;
  - add a polkit rule that lets the robot's user start `factory-reset.target`. Ubuntu's target already restarts the system.
- Follow-up, for the robot: what it will remember (faces, conversations) joins what the reset erases. The button comes with the body's buttons.

## References

- systemd: [systemd.io/FACTORY_RESET](https://systemd.io/FACTORY_RESET/) (read 2026-11-13 and 2026-11-19), [org.freedesktop.systemd1(5)](https://www.freedesktop.org/software/systemd/man/latest/org.freedesktop.systemd1.html) (`Subscribe`, `StartUnit`, `JobRemoved` and its results), [systemd-factory-reset(8)](https://www.freedesktop.org/software/systemd/man/latest/systemd-factory-reset.html), v259's `src/factory-reset/factory-reset-tool.c` and `src/shared/factory-reset.c`, the `systemd-factory-reset.socket` unit (`SocketMode=0666`), and [sd_id128_get_boot(3)](https://www.freedesktop.org/software/systemd/man/latest/sd_id128_get_machine.html); all read 2026-11-19.
- [Varlink](https://varlink.org/): "All messages are encoded as JSON objects and terminated with a single NUL byte" (read 2026-11-19).
- [fsync(2)](https://man7.org/linux/man-pages/man2/fsync.2.html), Linux man-pages 6.19: a file's entry in its folder is on the disk only once the folder is synced too (read 2026-11-19).
- [RFC 6455](https://www.rfc-editor.org/rfc/rfc6455), 4.1 and 4.2.2, and Tornado's `WebSocketHandler.check_origin`, v6.5.2 (read 2026-11-19).
- [XDG Base Directory Specification](https://specifications.freedesktop.org/basedir/latest/), 0.8 (read 2026-11-19).
- NetworkManager: [nm-settings-dbus(5)](https://networkmanager.dev/docs/api/latest/nm-settings-dbus.html), `user.data`, whose keys are dot-separated (read 2026-11-19).
- TCG TPM 2.0 Library, Part 3: Commands, r1.59, 24.6 (`TPM2_Clear`).
- ETSI EN 303 645, provision 5.11-1.
- Amazon's help pages for the Echo Show and Astro, and Meta's for the Portal, as cited in ai-platform ADR-0015.
