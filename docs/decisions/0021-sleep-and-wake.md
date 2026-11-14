# 0021 — Sleep and wake: the robot decides, the kiosk turns the screen off and on

- **Status:** Accepted.
  - The behaviour is the owner's choice (2026-11-14):
    - the wake sources;
    - the screen never turns off while something goes on;
    - "Sleep after" 1, 2, 5, 10 or 30 minutes, or never, with 5 minutes as shipped;
    - no night mode yet.
  - The helper that can only press one key is the owner's choice too (2026-11-14).
  - The rest was delegated: how the screen is kept on and woken, Frame's settings, the protocol field, and the messages.
- **Date:** 2026-11-14

## Context

- The robot's screen is an Ubuntu Frame kiosk (Frame 24/stable, Mir 2.26.0) that runs Chromium 156, the snap's daemon, on the page the robot serves.
- Mir 2.26.0 turns the outputs off after `idle-timeout` seconds without input (`src/server/shell/basic_idle_handler.cpp`). It has no Wayland protocol that lets a client turn an output off or on.
- Mir's source (tag v2.26.0, read 2026-11-14) gives three facts:
  - Any key, pointer or touch event restarts Mir's idle timer and turns a dark screen on. It is delivered to the focused window first (`src/server/input/idle_poking_dispatcher.cpp`).
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
  - The page receives the key as a `keydown` with `key` `"WakeUp"`.
- Prior art (read 2026-11-14):
  - Echo Show turns its screen on "when they detect the wake word or motion in view of the camera, or when you interact with the devices' screens or buttons" (Amazon's Echo Show FAQ, amazon.com help node 201602230, from a search excerpt: the page answered HTTP 503).
  - Meta Portal sleeps after 15 minutes without use, and touch, voice, motion or a call wakes it (meta.com/help/portal/672369337507293, from a search excerpt).
  - Nest Hub: "Wake up your screen: Tap it or say 'Hey Google.'" (support.google.com/googlehome/answer/9137285).
  - Android keeps the screen on only while an app in the foreground asks for it (`FLAG_KEEP_SCREEN_ON`, developer.android.com, "Keep the screen on"). Its timeouts run from 15 s to 30 min (AOSP Settings, `screen_timeout_entries`).

## Decision

- **The robot decides when it sleeps.** The screen module counts the minutes since the last activity. After "Sleep after", it marks the state `asleep`.
  - Nothing counts while something goes on:
    - a conversation;
    - a scene that keeps it awake: set-up's while set-up is open, and those the screen's vocabulary marks `keeps_awake`, such as the timer;
    - an open panel, such as the settings.
  - The countdown starts again when that ends.
- **One way to wake.** Every wake source publishes `WakeUp(source, reason)`, with `reason` one of `touch`, `wake_word`, `presence`, `event` or `app`.
  - The screen publishes `touch` for every tap, hold and panel action.
  - A tap on a sleeping robot only wakes it, as a tap wakes Nest Hub: the face it lands on was not showing, so it does not act on it.
  - The voice module publishes `wake_word` when it hears its wake word.
  - A hold that starts while the robot sleeps wakes it as `event`.
  - Presence (a camera) and the owner's app plug into the same message when they exist.
- **The page keeps the screen on.** The screen protocol's `state` carries `asleep`.
  - While awake, the page holds a Screen Wake Lock, the web's standard way to keep the screen on, and asks again whenever the page shows again.
  - Asleep, the face grows sleepy, closes its eyes, and then the page releases the lock. Frame turns the screen off `idle-timeout` seconds later.
  - Frame's `idle-timeout` is therefore the time from closed eyes to a dark screen. It is also the screen's limit when no page holds a lock (15 s, `tools/pi/kiosk/frame.config`).
- **A key wakes a dark screen.** The robot's `display_power` port wakes it. It does so when the robot wakes from anything but a touch (Mir has lit the screen already), and when a page connects while the robot is awake (Frame may have turned the screen off before the page loaded).
  - On the Pi, the port asks a helper, `ai-robot-wake`, over a Unix socket. The helper presses `KEY_WAKEUP` on a uinput keyboard that has no other key, at most once per interval.
  - The helper runs as a dynamic user with only the `uinput` group, from a socket that only root and the robot's user's group may use. It is hardened (`tools/pi/kiosk/ai-robot-wake.service`, which `systemd-analyze security` scores 0.7, "SAFE").
  - Its program is a copy of `software/src/ai_robot/adapters/wake/helper.py` in `/usr/local/libexec`, which only root may change.
  - A press that fails ends the helper, and systemd starts it again with a new keyboard.
  - The robot's own user is in neither the `uinput` group nor the `input` group. The members of `input` can write events into any input device, the touch screen's included: `evdev_write` passes them to `input_inject_event` (Linux, `drivers/input/evdev.c`, read 2026-11-14).
- **The kiosk's set-up is in `tools/pi/`:**
  - Frame's config gains `add-wayland-extensions=zwp_idle_inhibit_manager_v1`;
  - Chromium stops and starts again with Frame, and starts again whenever it exits. It exits with status 0 when Frame goes away, which the snap's `Restart=on-failure` left down;
  - the helper, its socket, the uinput rule and the robot's unit.

## Why

- **The robot decides,** because only it knows what goes on (a conversation, a code on screen, a timer) and when the face should grow sleepy. Mir's timer cannot show a sleepy face before the screen goes dark. Mir's timer also lives in Frame's config, which root sets and which restarts Frame when it changes.
- **The wake lock,** because it is the browser's own way to keep a screen on. It works on any compositor that offers idle inhibition (Mir, wlroots, GNOME, KDE).
  - Releasing it lets the compositor's own power handling do the rest, as Android lets the screen turn off once no window asks.
  - It needs no privilege, so a renderer on another device needs nothing more.
- **A synthetic key,** because input is the only thing that lights a dark screen in Mir 2.26.
  - `KEY_WAKEUP` is the key meant for this. Chromium has no action for it.
  - uinput works with any compositor, and with X too.
- **A helper that presses one key, not uinput for the robot's user:**
  - The `uinput` group could create any device and type any key into the kiosk's Chromium, which runs as root without its sandbox (the snap's launcher adds `--kiosk --no-sandbox` to its daemon, Chromium snap 156.0.8078.47).
  - A helper with one key and a rate limit gives the robot only what it needs. GNOME's `gsd-backlight-helper` is built the same way: it only sets the backlight.
  - The socket's group is the robot's user's own. A group of its own would add nothing: that user owns the tree the robot runs from.

## Options not taken

- **Mir's `idle-timeout` as "Sleep after".** No sleepy face before the screen goes dark. Each change needs root and restarts Frame, and nothing stops the timer while a timer runs on screen.
- **The robot as a Wayland client with a virtual keyboard or pointer.** It would need Frame's socket, which is root's, and the extension would also be offered to Chromium.
- **uinput access for the robot's user.** Turned down as above.
- **An output power protocol in Mir** (`wlr-output-power-management-unstable-v1`, which wlroots compositors and Raspberry Pi OS's `wlopm` use). It would let the robot turn the screen off and on directly. It would also let the robot choose how long the eyes stay closed, without Frame's `idle-timeout`. Mir 2.26 lacks it.
  - **Proposal for the owner:** offer Mir upstream an implementation of `zwlr_output_power_manager_v1` (github.com/canonical/mir), as a pull request under the owner's account. If Mir ships it, the `display_power` port gains an adapter that uses it, and the helper goes.
  - Not done: it needs the owner's approval, and it waits on Mir's maintainers and a Frame release.

## Consequences

- Waking takes a fraction of a second on the Pi, and the page sees one `WakeUp` key.
- The face's look while asleep, and the delays before the eyes close and before the lock is released, are the renderer's (`ui/src/settings.js`: 3 s of a sleepy face, then 2 s of closed eyes, delegated). Frame's delay is the kiosk's (`tools/pi/kiosk/frame.config`).
- Changing Frame's config restarts Frame (its configure hook), and Chromium with it.
- If Frame's config loses the extension, the screen turns off `idle-timeout` seconds after every touch. `tools/pi/kiosk.sh check` reports it.
- A developer's browser showing the face also holds a wake lock while the robot is awake.
- Night mode (Nest Hub's Low-light mode) is not part of this. It would be one more state of the same screen module.
