# 0021 — Sleep and wake: the robot decides, the kiosk turns the screen off and on

- **Status:** Accepted.
  - The behaviour is the owner's choice (2026-11-14):
    - the wake sources;
    - the screen never turns off while something goes on;
    - "Sleep after" 1, 2, 5, 10 or 30 minutes, or never, with 5 minutes as shipped;
    - no night mode yet.
  - The helper that can only press one key is the owner's choice too (2026-11-14).
  - The rest was delegated: how the screen is kept on and woken, Frame's settings, the protocol field, and the messages.
  - A touch on the dark screen lights it through the helper (2026-11-16): the approach was the coordinator's, the details delegated.
- **Date:** 2026-11-14

## Context

- The robot's screen is an Ubuntu Frame kiosk (Frame 24/stable, Mir 2.26.0) that runs Chromium 156, the snap's daemon, on the page the robot serves.
- Mir 2.26.0 turns the outputs off after `idle-timeout` seconds without input (`src/server/shell/basic_idle_handler.cpp`). It has no Wayland protocol that lets a client turn an output off or on.
- Mir's source (tag v2.26.0, read 2026-11-14 and 2026-11-16) gives four facts:
  - Any key or pointer event, and any touch on a screen that is on, restarts Mir's idle timer and turns a dark screen on. It is delivered to the focused window first (`src/server/input/idle_poking_dispatcher.cpp`).
  - A touch on a screen that is off goes nowhere. Mir passes a touch frame on only while the touch screen's output is active (`src/platforms/evdev/libinput_device.cpp`, `is_output_active()`, for a touch screen mapped to an output, the default), and an output is active only while its power is on (`src/server/input/basic_seat.cpp`, under the upstream note "TODO make the decision whether display that is used but powered off should emit touch screen events in a policy"). No setting changes it: MirAL does not expose the touch screen's mapping mode, and Frame's options have none (`docs/reference/ubuntu-frame-configuration-options.md`, read 2026-11-16).
  - `zwp_idle_inhibit_manager_v1` keeps the outputs on while the inhibiting surface is visible (`src/server/frontend_wayland/idle_inhibit_v1.cpp`). Releasing an inhibitor restarts the idle timer. Mir supports this protocol but does not offer it by default: it is missing from `get_standard_extensions()`, and `add-wayland-extensions` adds it.
  - Its virtual keyboard and pointer protocols are not offered by default either. Frame's Wayland socket is in root's runtime directory, so the robot's user could not reach them anyway.
- Chromium's Screen Wake Lock becomes a `zwp_idle_inhibitor_v1` on the focused window (`ui/ozone/platform/wayland/host/zwp_idle_inhibit_manager.cc`, M156). `http://127.0.0.1` counts as a secure context (W3C Secure Contexts).
- On the robot's Pi, on 2026-11-14 (`tools/pi` set-up, idle timeout 15 s):
  - **Without** `add-wayland-extensions`, Frame offers no idle-inhibit manager. Chromium grants the wake lock, but the screen turns off 15 s after the last input anyway.
  - **With** `add-wayland-extensions=zwp_idle_inhibit_manager_v1`:
    - a held lock kept the screen on throughout;
    - releasing it turned the screen off 15.4 s later;
    - a lock asked for while the screen was dark did not light it;
    - a `KEY_WAKEUP` key from a uinput device lit it in 0.08–0.21 s, and the waiting lock then kept it on.
  - The page stays `visible` and focused while the screen is dark. Chromium does not learn that Mir hid it.
  - The page receives the key as a `keydown` with `key` `"WakeUp"`. Its `code` is `"WakeUp"` too: Chromium maps Linux's `KEY_WAKEUP` to it (`ui/events/keycodes/dom/dom_code_data.inc`), and Frame passes every key on to the window (`FrameWindowManagerPolicy::handle_keyboard_event`).
- On the robot's Pi, on 2026-11-16, with the screen dark: two minutes of touches sent about 30,000 events from the touch screen (`wch.cn USB2IIC_CTP_CONTROL`, `/dev/input/event1`), and neither Mir nor the robot woke. The kernel reported the display connector's `dpms` as `Off` throughout.
- Prior art (read 2026-11-14):
  - Echo Show turns its screen on "when they detect the wake word or motion in view of the camera, or when you interact with the devices' screens or buttons" (Amazon's Echo Show FAQ, amazon.com help node 201602230, from a search excerpt: the page answered HTTP 503).
  - Meta Portal sleeps after 15 minutes without use, and touch, voice, motion or a call wakes it (meta.com/help/portal/672369337507293, from a search excerpt).
  - Nest Hub: "Wake up your screen: Tap it or say 'Hey Google.'" (support.google.com/googlehome/answer/9137285).
  - Android keeps the screen on only while an app in the foreground asks for it (`FLAG_KEEP_SCREEN_ON`, developer.android.com, "Keep the screen on"). Its timeouts run from 15 s to 30 min (AOSP Settings, `screen_timeout_entries`).

## Decision

- **The robot decides when it sleeps.** The screen module counts the minutes since the last activity. After "Sleep after", it marks the state `asleep`.
  - Nothing counts while something goes on:
    - a conversation;
    - a scene that keeps it awake: set-up's while set-up is open, and those the screen's vocabulary gives a `keeps_awake_until`: the timer, until its `ends_at`, when it rings;
    - an open panel, such as the settings.
  - The countdown starts again when that ends. A timer left ringing therefore lets the robot sleep a whole "Sleep after" after it rang.
- **One way to wake.** Every wake source publishes `WakeUp(source, reason)`, with `reason` one of `touch`, `wake_word`, `presence`, `event` or `app`.
  - The screen publishes `touch` for every tap, hold and panel action.
  - A tap on a sleeping robot only wakes it, as a tap wakes Nest Hub: the face it lands on was not showing, so it does not act on it.
  - The voice module publishes `wake_word` when it hears its wake word.
  - Something going on that starts while the robot sleeps, such as a conversation, a scene that keeps it awake or a panel, wakes it as `event`.
  - Presence (a camera) and the owner's app plug into the same message when they exist.
- **The page keeps the screen on.** The screen protocol's `state` carries `asleep`.
  - While awake, the page holds a Screen Wake Lock, the web's standard way to keep the screen on, and asks again whenever the page shows again.
  - Asleep, the face grows sleepy, closes its eyes, and then the page releases the lock. Frame turns the screen off `idle-timeout` seconds later.
  - Frame's `idle-timeout` is therefore the time from closed eyes to a dark screen. It is also the screen's limit when no page holds a lock (15 s, `tools/pi/kiosk/frame.config`).
  - Without its link to the robot for a grace period, the page releases the lock too, and Frame turns the screen off as if the robot slept. A touch still lights it. A robot that comes back awake says so in its first state: the page takes the lock again, and the robot lights the screen, as for any page that connects. The grace period is the renderer's (`ui/src/settings.js`), longer than a restart of the robot's software, so that a restart leaves the screen on. This was the coordinator's decision, delegated, on 2026-11-16: a page that kept its lock with no robot behind it kept a frozen face lit for good.
- **A key wakes a dark screen.** The robot's `display_power` port wakes it. It does so when the robot wakes from anything but a touch (Mir has lit the screen already), and when a page connects while the robot is awake (Frame may have turned the screen off before the page loaded).
  - On the Pi, the port asks a helper, `ai-robot-wake`, over a Unix socket. The helper presses `KEY_WAKEUP` on a uinput keyboard that has no other key, at most once per interval.
  - The helper runs as a dynamic user with only the `uinput` group, from a socket that only root and the robot's user's group may use. It is hardened (`tools/pi/kiosk/ai-robot-wake.service`, which `systemd-analyze security` scores 0.7, "SAFE").
  - Its program is a copy of `software/src/ai_robot/adapters/wake/helper.py` in `/usr/local/libexec`, which only root may change.
  - A press that fails ends the helper, and systemd starts it again with a new keyboard.
  - The robot's own user is in neither the `uinput` group nor the `input` group. The members of `input` can write events into any input device, the touch screen's included: `evdev_write` passes them to `input_inject_event` (Linux, `drivers/input/evdev.c`, read 2026-11-14).
- **A touch on the dark screen presses the key too.** Mir drops that touch, so the helper also reads the touch screens, beside Mir and without grabbing them. When a touch begins (`BTN_TOUCH` at 1) while no connected display is on, it presses `KEY_WAKEUP`, within the same rate limit.
  - Whether a display is on is the kernel's: a connector whose `status` is `connected` and whose `dpms` is `On` (`/sys/class/drm`, `drivers/gpu/drm/drm_sysfs.c`). A writeback connector says `unknown` and `On`, and does not count. When the helper cannot tell, it presses: a key for nothing costs nothing, and a key not pressed leaves the screen dark.
  - Mir lights the screen and passes the key to Chromium. The page, while the robot sleeps, reports it as `wake`, and the robot wakes as from a tap: it holds the lock again, and the screen stays on. Awake, the page ignores the key: the robot lit the screen itself.
  - The touch itself is lost, as a tap on a sleeping robot only wakes it anyway.
  - The helper may read the touch screens and no other input device. A udev rule gives them to the `touchscreen` group, which only the helper has, to read (`tools/pi/kiosk/70-ai-robot-touchscreen.rules`). Its unit may read input devices and write none (`DeviceAllow=char-input r`).
  - It looks for touch screens again every 2 s (`--rescan`), so it takes up one that USB enumerates again, and any number of them. Delegated, 2026-11-16.
- **The kiosk's set-up is in `tools/pi/`:**
  - Frame's config gains `add-wayland-extensions=zwp_idle_inhibit_manager_v1`;
  - Chromium starts whenever Frame starts, through a drop-in on Frame's unit that wants Chromium's. snapd stops Frame's services and starts them again to refresh the snap, and systemd passes only a stop or a restart on to Chromium (`PartOf=`), never a start (systemd.unit(5), read 2026-11-16);
  - Chromium stops and restarts with Frame, and starts again whenever it exits on its own. It exits with status 0 when Frame goes away, which the snap's `Restart=on-failure` left down. A unit stopped by systemd is not restarted (`Restart=`, systemd.service(5), read 2026-11-16);
  - the helper, its socket, the uinput and touch screen rules, the `touchscreen` group, and the robot's unit;
  - the robot's user leaves the `uinput` and `input` groups. A login reads the user's groups once (initgroups(3)), and its processes keep them until they end, the user's systemd manager among them. `kiosk.sh` therefore prints a last step, to end that user's sessions (`loginctl terminate-user`) or reboot, and `kiosk.sh check` reports a process that still holds either group. It does not end them itself: one of them may be the session of the person running it.

## Why

- **The robot decides,** because only it knows what goes on (a conversation, a code on screen, a timer) and when the face should grow sleepy. Mir's timer cannot show a sleepy face before the screen goes dark. Mir's timer also lives in Frame's config, which root sets and which restarts Frame when it changes.
- **The wake lock,** because it is the browser's own way to keep a screen on. It works on any compositor that offers idle inhibition (Mir, wlroots, GNOME, KDE).
  - Releasing it lets the compositor's own power handling do the rest, as Android lets the screen turn off once no window asks.
  - It needs no privilege, so a renderer on another device needs nothing more.
- **A synthetic key,** because input is the only thing that lights a dark screen in Mir 2.26.
  - `KEY_WAKEUP` is the key meant for this. Chromium has no action for it.
- **The helper reads the touch screens,** because nothing else hears a touch on a dark screen, and no setting makes Mir pass it on (above).
  - It looks for touch screens by reading `/dev/input` again every few seconds, not by inotify or udev's netlink: those need ctypes or a parser of udev's messages, and its sandbox allows Unix sockets only. A touch screen that USB enumerates again is back within that time.
  - It presses only while the screen is dark: a lit screen passes the touch on, and the page reports it as a tap.
  - The `touchscreen` group reads and cannot write: with `input`, the helper could type into the kiosk too. `input` has no member on the robot, so the touch screens leaving it takes nothing from anyone.
  - uinput works with any compositor, and with X too.
- **A helper that presses one key, not uinput for the robot's user:**
  - The `uinput` group could create any device and type any key into the kiosk's Chromium, which runs as root without its sandbox (the snap's launcher adds `--kiosk --no-sandbox` to its daemon, Chromium snap 156.0.8078.47).
  - A helper with one key and a rate limit gives the robot only what it needs. GNOME's `gsd-backlight-helper` is built the same way: it only sets the backlight.
  - The socket's group is the robot's user's own. A group of its own would add nothing: that user owns the tree the robot runs from.

## Options not taken

- **A Mir that passes on touches from a dark screen.** Mir passes them when the touch screen is mapped to the whole display wall, not to its output (`mir_touchscreen_mapping_mode_to_display_wall`), but nothing Frame offers sets it. A Mir server of our own would. Making it a policy, as Mir's own note asks, would be an upstream change under the owner's account. Not proposed (2026-11-16): the helper does the work with no change upstream.
- **The helper telling the robot of the touch over its own socket.** A second path, and a connection that stays open, for what the key already carries to the page.

- **Mir's `idle-timeout` as "Sleep after".** No sleepy face before the screen goes dark. Each change needs root and restarts Frame, and nothing stops the timer while a timer runs on screen.
- **The robot as a Wayland client with a virtual keyboard or pointer.** It would need Frame's socket, which is root's, and the extension would also be offered to Chromium.
- **uinput access for the robot's user.** Turned down as above.
- **An output power protocol in Mir** (`wlr-output-power-management-unstable-v1`, which wlroots compositors and Raspberry Pi OS's `wlopm` use). It would let the robot turn the screen off and on directly. It would also let the robot choose how long the eyes stay closed, without Frame's `idle-timeout`. Mir 2.26 lacks it.
  - **Proposal for the owner:** offer Mir upstream an implementation of `zwlr_output_power_manager_v1` (github.com/canonical/mir), as a pull request under the owner's account. If Mir ships it, the `display_power` port gains an adapter that uses it, and the helper goes.
  - Not done: it needs the owner's approval, and it waits on Mir's maintainers and a Frame release.

## Consequences

- Waking takes a fraction of a second on the Pi, and the page sees one `WakeUp` key.
- A touch on the dark screen lights it once the helper reads it, and wakes the robot once the page reports the key. A touch screen that comes back after USB enumerates it again is read again within 2 s.
- The face's look while asleep, and the delays before the eyes close and before the lock is released, are the renderer's (`ui/src/settings.js`: 3 s of a sleepy face, then 2 s of closed eyes, and 30 s of a link down, delegated). Frame's delay is the kiosk's (`tools/pi/kiosk/frame.config`).
- Changing Frame's config restarts Frame (its configure hook), and Chromium with it. A refresh of Frame's snap stops and starts Frame, and Chromium with it.
- If Frame's config loses the extension, the screen turns off `idle-timeout` seconds after every touch. `tools/pi/kiosk.sh check` reports it.
- A developer's browser showing the face also holds a wake lock while the robot is awake.
- Night mode (Nest Hub's Low-light mode) is not part of this. It would be one more state of the same screen module.
