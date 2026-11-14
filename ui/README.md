# ui/ — the robot's screen

> **Status:** the face works on any screen and the robot software drives it: 14 expressions, 6 scenes (clock, weather, timer, charging, set-up, start-up) and the settings panel. 30 of the previous library's 33 scene categories are still to port. Architecture: [ADR-0006](../docs/decisions/0006-screen-renderer.md).

The renderer that draws the robot's face. On the robot, a full-screen browser shows `screen.html`, served and driven by the robot software over the [screen protocol](../protocol/screen/README.md). The software says **what** to show: an expression, a scene, the gaze, the voice level, the battery, and panels such as the settings. The renderer decides **how**: it blends from one expression to the next and keeps the face alive on its own, with blinking, glances and breathing. A scene (the time, the weather, a timer…) fills the screen while the face watches from a corner, keeping its expression. A panel is drawn in HTML over the face, and the owner works it by touch ([ADR-0016](../docs/decisions/0016-settings.md)); a press held still on the face asks the robot for its settings.

Plain JavaScript modules drawing on a 2D canvas: no build step, no framework, nothing to install. The same files run on the robot, in any browser and in the tests. For hosts that refuse separate script files, such as a claude.ai Artifact, `tools/single_file.py` inlines them into one page.

When the robot's page opens, it plays its start-up (a line of light opening into the eyes) to the end before it shows the software's state. It reloads itself when the software serves a new revision of these files, and shows the time on the software's clock.

## See it

The pages load JavaScript modules, which browsers refuse from `file://`, so serve the repository over HTTP:

```sh
python3 -m http.server 8000
```

- <http://localhost:8000/ui/> — the **studio**: every expression and scene on any screen, driven by hand (expression, scene and its data, colour, gaze, voice, life layers, slow motion, the status row), and the settings panel at the screen's own size. `#happy`, `#weather` or `#round-360` at the end of the address opens it on that expression, scene or screen.
- <http://localhost:8000/ui/tests/runtime.html> — the tests, and every expression and scene side by side.

To take the studio elsewhere, build it as one file that opens without a server, from disk or attached to a message (`--fragment` leaves out the document skeleton, for hosts that add their own, such as claude.ai Artifacts). Modules must then keep to the import and export forms the tool's header lists; CI runs the tests on a one-file build of their page to check it:

```sh
python3 ui/tools/single_file.py --out studio.html
```

With the robot software running (`ai-robot --profile desktop`, see [software/](../software/README.md)), its screen is at <http://127.0.0.1:8770/>. On the robot, Ubuntu Frame shows that address in Chromium, in kiosk mode ([ADR-0017](../docs/decisions/0017-ros-2.md); [a web kiosk with Ubuntu Frame](https://ubuntu.com/frame/docs/26/tutorial/make-a-secure-ubuntu-web-kiosk/)). Chromium is also the engine that runs the tests below. Add `#round` to the address on a round display.

## How it fits together

```text
software ──state, look, voice──▶ link.js ──▶ animator.js ──▶ draw.js ──▶ canvas
         ◀──ready, touch, hold──    │                │
         ◀──action, close──────┐    │   expressions.js · motions.js · effects.js · pose.js · scenes/
                               │    ▼
                               └─ panel.js: panels in HTML, over the canvas
```

| File | Role |
|---|---|
| `src/pose.js` | The face model: two eyes (size, corners, lids, pupils, brows) and a mouth, all numbers |
| `src/expressions.js` | Expressions as data: the numbers that differ from rest, a colour, a motion, an effect, how much life |
| `src/motions.js` | What gives an expression its movement: bounce, sway, shake, droop… |
| `src/effects.js` | What an expression draws around the eyes: hearts, a tear, Zzz… |
| `src/scenes/` | Scenes: one file each (`clock.js`, `weather.js`, `timer.js`, `charging.js`, `setup.js`, `boot.js`), the registry with `sceneTone`/`sceneSlot` (`index.js`), and shared tools (`kit.js`: the face's corner, time, the battery, shapes, a cache for static drawing) |
| `src/text.js`, `src/math.js` | Text (one font, steady digits, cutting by characters) and small maths shared by every module |
| `src/animator.js` | Blends expressions and scenes over time, moves the face to a scene's corner, and adds the life layers: breathing, glances, gaze, voice, blinking |
| `src/draw.js` | Draws a frame: scenes, face, glow, effects, status row |
| `src/layout.js` | Fits the face to any screen size and shape; the studio's screen profiles |
| `src/player.js` | Runs an animator on a canvas, at the display's refresh rate |
| `src/link.js` | The screen protocol: applies the software's messages, reports the page, its taps and holds, and what the owner does on a panel |
| `src/sleep.js` | The robot's sleep on the page: a Screen Wake Lock while the robot is awake; asleep, a sleepy face, closed eyes, and then the lock let go so that the screen turns off ([ADR-0021](../docs/decisions/0021-sleep-and-wake.md)) |
| `src/panel.js` | Draws a panel the software describes, such as the settings, in HTML over the face: switches, sliders, choices, buttons and lines to read, with a question before what asks for one |
| `src/press.js` | Tells a press held still, a hold, from a tap |
| `src/tones.js` | The colour palette |
| `src/settings.js` | How the face moves and responds, in one place: transitions, blinks, breathing, glances, gaze, voice, holds, panels, falling asleep |
| `screen.html` | The robot's screen |
| `index.html`, `src/studio.js` | The studio |
| `tests/` | The tests |
| `tools/single_file.py` | Builds the studio as one HTML file, every module inlined |
| `third_party/` | Code from other projects, with where it comes from: a QR code generator, for the set-up scene |
| `legacy/` | The previous library: 256 designs to port from, then delete ([legacy/README.md](legacy/README.md)) |

## Screens

A rectangular screen keeps a status row along its top, a twelfth of its height: the time, the battery and, while the microphones are off, a struck microphone. The face fills the rest at the largest scale that keeps its proportions: on the previous generation's 320 × 240 screen it is exactly its design size, and on the 7" 1024 × 600 LCD it is 2.5 times larger. A round screen centres the face on the disc, at design size on a 360 px disc, and draws inside the disc only; a panel keeps to the square inside the disc. The face is designed in face units on a 320 × 220 area (`src/pose.js`).

## Editing

- **An expression:** add or change an entry in `src/expressions.js`; the studio shows it at once. Use the numbers of `src/pose.js`, a tone of `src/tones.js`, a motion of `src/motions.js`, an effect of `src/effects.js`. An expression the software relies on must also be listed in [`protocol/screen/vocabulary.json`](../protocol/screen/vocabulary.json).
- **A motion or an effect:** a function in `src/motions.js` or `src/effects.js`; both take the seconds since the expression started.
- **A scene:** a file in `src/scenes/`, registered in `src/scenes/index.js` (its header lists what a scene gives). Draw from the data the software sends, never from canned content, and cope with missing or malformed data. Keep clear of the face's corner (`CORNER` in `kit.js`), and inside a 180-unit radius so that it fits round screens. Draw what never changes through `cached()` in `kit.js`: the glow is the most expensive part of a frame, and the cache pays for it once. A scene the software relies on must also be listed, with its data, in the protocol's vocabulary, and the tests compare what the scene accepts with it.
- **A panel:** the software describes it, so a new setting needs no change here. A new kind of item goes in the protocol's vocabulary first, then in `src/panel.js`.
- **How the face moves:** tune it in `src/settings.js`, never with numbers in the code. Shapes, motions and colours are content, in the files above. What the robot decides, such as when the battery is low, comes from the software.
- **The protocol:** change [`protocol/screen/`](../protocol/screen/README.md) first, then `src/link.js` and the software, in the same pull request.
- Keep the animator deterministic: time is passed in, and randomness comes from its seeded generator, never from `Math.random`.

## Test

Open <http://localhost:8000/ui/tests/runtime.html> with the server above running, or run it headless:

```sh
google-chrome --headless=new --virtual-time-budget=20000 \
  --dump-dom http://localhost:8000/ui/tests/runtime.html > result.html
python3 ui/tests/result.py result.html
```

The tests check the expressions and scenes against the protocol's vocabulary, draw every expression and every scene (with good, missing and malformed data) on every screen profile, and check the animator: the same calls give the same frames, transitions never jump, scenes move the face smoothly, protocol messages land where they should. They work a panel as an owner would (every kind of item, questions, updates in place, a refused value going back, closing when left untouched), and check holds and the status row. `data-status="pass"` means every check passed; otherwise the page lists each failure. CI runs them, with the previous library's smoke test, on every change under `ui/` or `protocol/screen/` ([workflow](../.github/workflows/ui.yml)).
