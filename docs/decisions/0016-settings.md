# 0016 — Settings: declared by the modules, shown as a panel on the touch screen

- **Status:** Accepted
  - Amended by [ADR-0023](0023-factory-reset.md): an action is pressed, never sent again from what was kept.
- **Date:** 2026-10-25

## Context

- The touch screen is a full user interface: settings and other interactive screens, opened by a tap or by voice ([vision](../vision.md)). Today the screen only shows: expressions and scenes go out, taps come back ([the screen protocol](../../protocol/screen/README.md)).
- Several requirements need settings at the robot:
  - **SEC-9:** the owner switches capability classes off locally, "with a setting the cloud cannot change";
  - **PRIV-1:** a mute control cuts the microphones;
  - **IO-4:** the volume and the mute are part of a turn's context;
  - **PROV-1:** set-up may happen on the touch screen. A bound robot has no way yet to open set-up again, to give a new Wi-Fi network ([ADR-0015](0015-set-up.md));
  - **IO-12:** the cloud sends settings, validated by the robot, and **PRIV-6:** a factory reset erases them.
- What one setting is changes from module to module: the volume is `voice`'s, the classes are `cloud`'s, set-up is `setup`'s. The capabilities of the cloud already work this way: each module offers its own, and the `cloud` module only gathers them (EXT-9).
- How others describe settings:

| System | How |
|---|---|
| iOS Settings bundles | A property list of specifiers, drawn by the Settings app: groups, toggles, sliders, lists of values, read-only titles. |
| Android | Preference screens: categories, switches, seek bars, lists, plain preferences that act when tapped. |
| Home Assistant | Set-up and options flows: the server sends a step's schema, and the frontend draws the form from it, with its texts from translations. |
| OpenVoiceOS | Skills send pages and their data over a GUI message bus, and the GUI app draws them. |

## Decision

1. **Modules declare their settings on the bus**, as they offer capabilities:
   - a value, with a JSON Schema: a boolean, a number between two bounds, or one of a list;
   - an action, such as "set up with the app";
   - or a line of information, such as the software's version.

   Each setting is named after its module (`voice.volume`), belongs to a section, and has a title. The owner applies a change, and checks it against its schema first.
2. **A `settings` module keeps what the owner chose**, in its `settings` port: a file on the data partition ([ADR-0014](0014-updates.md)), which a factory reset erases.
   - The configuration holds the values as shipped; the kept ones replace them.
   - It sends the kept values at start, and the owners apply them before anything uses them. Only values: an action is pressed on the panel (`SettingPressed`), so nothing kept does one, whatever the file holds. The `cloud` module therefore offers the cloud only the classes the owner allowed, from the first session (SEC-9).
3. **The touch screen shows them as a panel, which the software describes**, as it describes scenes. The software sends sections of items: toggles, sliders, choices, buttons and lines of information. The renderer draws them over the face, in HTML, and reports what the owner does. It keeps no settings of its own.
   - Touch targets are at least 48 pixels, Material's guidance, which is above WCAG 2.2's 44 for its enhanced level (2.5.5).
   - Switching on what widens the cloud's reach asks for confirmation.
   - A change shows at once, on the screen and in the panel the `settings` module describes, before the owner of the setting applies it. If the owner refuses it, the panel is sent again as it is, and the value goes back.
4. **A long press on the face opens it**, and so does the cloud's `settings.open`, so that "open the settings" works by voice. The panel closes on "Done", or after a minute without a touch.
5. **The first settings:**
   - **Wi-Fi and app:** open set-up, and the network the robot is on;
   - **sound:** the volume, and the microphones on or off;
   - **what the cloud may do:** each safety class, which only the owner, at the robot, can change (SEC-9);
   - **about:** the robot's name, its software, its identifier, whether it is set up.
6. **The cloud's settings (IO-12) come later**, through capabilities, with the same schemas. The safety classes will never be among them.

## Alternatives considered

| Alternative | Why not |
|---|---|
| A settings app in the renderer, which calls an API of the software | Richer screens at once. But the logic would be split between JavaScript and Python, and the renderer would stop being a view of what the software decides. |
| One file that lists every setting | A setting would live far from the code that applies it, and every new setting would touch it. Offers on the bus already keep the capabilities with their modules. |
| Drawing the panel on the canvas, like the face | One look throughout. But scrolling, focus, reading aloud and text input would all be made by hand; HTML brings them. |
| Numbering each action, and keeping the renderer's change until a state answers that number, as games reconcile a player's moves with the server | Exact even for a state already on its way when the owner touches, which can show the old value for a moment. But every module would have to report when it has applied a change. Showing the change at once in the software, and sending the panel again on a refusal, gives the same screen with nothing to track. |
| Settings in the app only | Needs the phone, and SEC-9 asks for a setting the cloud cannot change: the robot's screen is where it lives. |

## Consequences

- Positive:
  - a module adds a setting without touching the screen or the `settings` module;
  - SEC-9 and the mute are met at the robot, and set-up can be opened again;
  - what the owner chose survives restarts and updates, and a factory reset erases it;
  - the renderer stays a view.
- Negative / accepted trade-offs:
  - the renderer gains an HTML layer, with its own tests;
  - the texts are English and come from the software: translating them comes later, where they are made;
  - a child can open the panel with a long press. Widening the cloud's reach asks for confirmation; an owner's PIN may follow.
- Follow-up:
  - the Wi-Fi on the screen itself, which needs a keyboard (PROV-1);
  - a factory reset in the panel, with the image's data partition ([ADR-0014](0014-updates.md));
  - the cloud's settings (IO-12);
  - whether the mute also cuts the microphones in the voice processor, to be checked on the board ([ADR-0011](0011-audio-voice-processor.md)).

## References

Retrieved 2026-10-25:

- [Implementing an iOS Settings Bundle](https://developer.apple.com/library/mac/documentation/cocoa/conceptual/UserDefaults/Preferences/Preferences.html), Apple; [Settings](https://developer.android.com/develop/ui/views/components/settings), Android Developers.
- [Data entry flow](https://developers.home-assistant.io/docs/data_entry_flow_index), Home Assistant developer docs; [ovos-gui](https://github.com/OpenVoiceOS/ovos-gui/blob/dev/README.md), OpenVoiceOS.
- Touch targets: [WCAG 2.2](https://www.w3.org/TR/WCAG22/), success criteria 2.5.5 and 2.5.8; Material's 48 dp, as quoted in [w3c/wcag#1831](https://github.com/w3c/wcag/issues/1831).
