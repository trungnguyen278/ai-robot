# Screen protocol

Between the robot software (`software/`: the `screen` module and the `web` adapter of its `screen` port) and the screen renderer (`ui/`: a page in a browser). Version **1** ([ADR-0006](../../docs/decisions/0006-screen-renderer.md)).

The software decides **what** the screen shows; the renderer decides **how**. The renderer runs the animation on its own (transitions, blinking, glances, breathing), so the face stays alive while the software is busy, and it lays the face out for whatever screen it runs on. The software therefore sends changes, never frames.

[`vocabulary.json`](vocabulary.json) holds the version, the expressions and scenes every renderer must draw, the data each scene takes (types, allowed values, lengths in characters), the colour names, and the kinds of items a panel holds. Both sides test against it (`software/tests/test_screen.py`, `test_system.py`, `test_web_screen.py`; `ui/tests/runtime.js`).

The renderer also plays a start-up animation of its own when a page opens (the `boot` scene), before it shows the software's state; it is not part of the protocol.

## Connection

- The software serves the renderer's files at `http://<host>:<port>/` (the page `screen.html` at `/`) and the WebSocket at `/ws` on the same address. The page connects to the address it was loaded from.
- Each WebSocket text frame carries one JSON object with a `type`. A side ignores types and fields it does not know, so either side can add optional fields without breaking the other.
- Several pages may connect at once (the robot's screen and a developer's browser); each receives every message.
- The software sends the whole state when a page connects and whenever it changes, and again, unchanged, when a panel is asked for again ([Panels](#panels)). A page that connects before the software has a state receives only `hello`, and the state when there is one. The page keeps no history: if the connection drops, it keeps animating the last state and reconnects, with growing delays up to 5 s.
- Numbers are finite (JSON has no NaN or infinity), and data is plain JSON: strings, numbers, booleans, null, arrays and objects with string keys. The software checks scene data when a request is made, not when it is sent.

## Software → renderer

### `hello`

Sent first, on every connection.

```json
{"type": "hello", "protocol": 1, "revision": "45dbb837332b4e9c", "time": 1792130400.25}
```

| Field | Meaning |
|---|---|
| `protocol` | The version. A renderer that speaks another one logs it and carries on with what it understands. |
| `revision` | A fingerprint of the renderer's files as the software serves them. A page that sees it change between two connections reloads itself, so that a software update never leaves an old page running. |
| `time` | The software's clock, in Unix seconds. The renderer reads the times it is sent (a timer's end) and shows the time of day on this clock, so a browser on another device agrees with the robot. |

### `state`

What to show. Sent on connection and on every change, with the software's `time` as in `hello`.

```json
{"type": "state",
 "face": {"expression": "listening", "tone": null},
 "scene": {"name": "weather", "data": {"condition": "rain", "temperature": 18}},
 "status": {"battery": 82.0, "charging": false, "low": false, "muted": false},
 "panel": null,
 "asleep": false,
 "time": 1792130401.5}
```

| Field | Meaning |
|---|---|
| `face.expression` | An expression of the vocabulary. The renderer blends into it from wherever the face is; an unknown name shows `neutral`. |
| `face.tone` | A colour of the vocabulary, or `null` for the expression's own. |
| `scene` | A scene of the vocabulary and its data, or `null` for none. The scene fills the screen while the face, keeping its expression, watches from a corner. New data for the scene on screen updates it without starting it again; an unknown name shows no scene. Optional: a missing field means `null`. |
| `status.battery` | Battery charge in percent, from 0 to 100, or `null` if unknown. |
| `status.charging` | `true`, `false`, or `null` if unknown. |
| `status.low` | `true` when the robot judges the battery low (the renderer shows it in red), `false`, or `null` if unknown. Optional: a missing field means `null`. The robot decides, by its own threshold; the renderer has none. |
| `status.muted` | `true` while the microphones are off: the status row shows it. `false`, or `null` if unknown. Optional: a missing field means `null`. |
| `panel` | An interactive panel over the face, such as the settings ([Panels](#panels)), or `null` for none. Optional: a missing field means `null`. |
| `asleep` | `true` while the robot sleeps ([Sleep](#sleep)), else `false`. Optional: a missing field means `false`. |

The scenes and their data:

| Scene | Data |
|---|---|
| `charging` | none: shows the battery from `status`. The software shows it for a few seconds when the robot is plugged in. |
| `clock` | none: the time of day on the software's clock |
| `setup` | `state`: `code` (the code and its QR code), `connected` (an app is setting the robot up), `connecting` (set up, waiting for the server) or `closed` (not set up: a tap opens set-up). With `code`: `code` (8 digits), `discriminator` (4 hexadecimal digits) and `link` (what the QR code holds, up to 256 characters). See the [set-up protocol](https://github.com/trungnguyen278/ai-platform/blob/main/protocols/setup/README.md), in ai-platform. |
| `timer` | `ends_at` (Unix time in seconds, on the software's clock, when it rings), `duration` (seconds), `label` (optional, up to 16 characters). The renderer counts down on its own, so the timer is sent once; at zero it pulses until the scene is taken away. |
| `weather` | `condition` (`clear`, `partly-cloudy`, `cloudy`, `rain`, `storm`, `snow`, `fog`), `temperature`, `unit` (`C` or `F`), `night` (optional, `true` for the moon), `place` (optional, up to 18 characters) |

A scene marked `robot_only` in the vocabulary, `setup`, is the robot's own: the cloud cannot ask for it, so that a set-up code on the screen is always the robot's.

A scene marked `keeps_awake`, `timer`, keeps the robot awake while it shows, as something going on: the software does not put the robot to sleep under it.

A renderer draws a scene even with missing or malformed data, with placeholders (a timer without `ends_at` shows `--:--`). A scene asked for again with new data is updated in place, without starting over.

### Sleep

The software decides when the robot sleeps, and says so with `asleep` ([ADR-0021](../../docs/decisions/0021-sleep-and-wake.md)).

- While `asleep` is `false`, a renderer that is the robot's screen holds a [Screen Wake Lock](https://www.w3.org/TR/screen-wake-lock/), so that the system keeps the screen on. It asks for it again whenever the page shows again.
- When `asleep` turns `true`, the face grows sleepy and then closes its eyes, with an expression of the renderer's own (`asleep`, outside the vocabulary). Then the renderer releases the lock, and the system turns the screen off a while later.
- While asleep, `face`, `scene` and `panel` are kept but not shown.
- When `asleep` turns `false`, the face, scene and panel show again, and the lock is held again.

A tap on a sleeping robot only wakes it: the software does not act on it. The software also lights a dark screen itself when the robot wakes from anything else, such as its wake word, and when a page sends `ready` while the robot is awake.

### Panels

A panel is a screen the owner acts on, drawn over the face: the settings, for one ([ADR-0016](../../docs/decisions/0016-settings.md)). The software describes it, as data; the renderer draws it, and reports what the owner does (`action`, `close`). The renderer keeps no state of its own beyond what is on screen.

```json
{"name": "settings", "title": "Settings", "sections": [
  {"title": "Sound", "items": [
    {"id": "voice.volume", "kind": "slider", "label": "Volume", "value": 80,
     "min": 0, "max": 100, "step": 5, "unit": "%"},
    {"id": "voice.microphones", "kind": "toggle", "label": "Microphones", "value": true,
     "help": "Off, the robot hears nothing"}]},
  {"title": "What the cloud may do", "items": [
    {"id": "cloud.physical", "kind": "toggle", "label": "Move the robot", "value": false,
     "confirm": "Let the cloud move the robot?"}]},
  {"title": "Wi-Fi and app", "items": [
    {"id": "setup.start", "kind": "button", "label": "Set up with the app"},
    {"id": "setup.network", "kind": "info", "label": "Wi-Fi", "value": "Home, connected"}]}]}
```

| Field | Meaning |
|---|---|
| `name` | Which panel. Actions name it. |
| `title` | Shown at its top. |
| `sections` | Its sections, in order: each a `title` and its `items`, in order. |
| `items[].id` | What an action names; unique in the panel. |
| `items[].kind` | `toggle` (on or off), `slider` (a number between two bounds), `choice` (one of several), `button` (an action), or `info` (a line to read). |
| `items[].label` | Its title. |
| `items[].help` | A line under the label. Optional. |
| `items[].value` | A toggle's boolean, a slider's number, the value of the choice made, or an info's text. A button has none. |
| `items[].min`, `max`, `step` | A slider's bounds and step. |
| `items[].unit` | Shown after a slider's value, such as `%`. Optional. |
| `items[].choices` | A choice's options: each a `value` and a `label`. |
| `items[].confirm` | A question the renderer asks before it switches a toggle on, or before a button acts. Optional. |

- The renderer shows the panel while `state.panel` is not `null`, over the face, which keeps animating. A panel sent again with the same `name` is updated in place, where the owner left it.
- A change shows at once. A state that takes it shows the panel with the new value; when the robot refuses it, the software sends the panel again as it is, even if nothing else changed, and the value goes back. While the owner drags a slider, the renderer leaves the slider where the finger is.
- While the connection is down, the renderer hides the panel, since nothing done on it would reach the robot; the state sent on reconnection shows it again if it is still open.
- Touch targets are at least 48 pixels high.
- After a while without a touch (the renderer's setting, a minute as shipped), the renderer closes the panel itself, with `close`.

### `look`

Where the eyes look. Sent on connection and on every change.

```json
{"type": "look", "target": {"x": 0.4, "y": -0.2}}
{"type": "look", "target": null}
```

`x` and `y` go from -1 to 1 across the screen as one faces it: (-1, -1) is the top left corner, (0, 0) straight ahead. `null` lets the eyes roam on their own.

### `voice`

The loudness of the voice the face reacts to, from 0 to 1: the person's voice while the robot listens; the robot's own voice while it speaks, once that is measured. Sent up to 25 times a second while it changes, then 0. The renderer treats a level older than half a second as silence.

```json
{"type": "voice", "level": 0.42}
```

## Renderer → software

### `ready`

Sent on every connection, after it opens.

```json
{"type": "ready", "protocol": 1, "width": 1024, "height": 600, "shape": "rect",
 "expressions": ["neutral", "listening", "…"], "scenes": ["boot", "clock", "…"]}
```

`width` and `height` are in CSS pixels; `shape` is `rect` or `round`. `expressions` and `scenes` list every expression and scene the renderer can draw, which may be more than the vocabulary requires. The software warns when it asks for one that no connected page knows.

### `touch`

A tap on the screen, as fractions of its width and height from the top left corner.

```json
{"type": "touch", "x": 0.42, "y": 0.61}
```

### `hold`

A press held still on the face for the renderer's hold time (3 s as shipped), where it began. The press was already reported as a `touch` when it began. The robot opens its settings on it.

```json
{"type": "hold", "x": 0.42, "y": 0.61}
```

### `action`

The owner acted on an item of a panel: switched a toggle, released a slider, made a choice (`value`, as the item takes it), or pressed a button (no `value`), after confirming if the item asks.

```json
{"type": "action", "panel": "settings", "item": "voice.volume", "value": 60}
{"type": "action", "panel": "settings", "item": "setup.start"}
```

### `close`

The owner closed a panel, or left it untouched for a while.

```json
{"type": "close", "panel": "settings"}
```

## Changing the protocol

Adding an optional field, a message type or an expression keeps the version: update this file and `vocabulary.json`, then both sides, in the same pull request. Changing or removing something either side relies on raises the version in `vocabulary.json`, `PROTOCOL` in `software/src/ai_robot/adapters/web/screen.py` and `PROTOCOL` in `ui/src/link.js`.
