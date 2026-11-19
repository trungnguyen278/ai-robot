// SPDX-License-Identifier: MIT
/* Scenes: pages of information that fill the screen while the face watches from a
   corner (clock, weather, timer…), and status screens (charging, start-up, set-up, the factory reset).

   Each scene gives:
   - `tone`: its colour (tones.js), or a function of its data that returns one;
   - `slot`: where the face waits, `{ x, y, scale }` in face units from the centre,
     or a function of the seconds since the scene started;
   - `clock: false` to fade out the status row's time while it is on screen;
   - `example`: sample data, for the studio and the tests;
   - `draw(ctx, t, look)`: draws it, `t` seconds after it started, on a context
     placed at the centre of the face area, in face units, with its colour and glow.
     `look` holds `alpha` (from 0 to 1 as it comes in and goes away), `data` (from
     the software), `now` (a Date on the software's clock), `status` (`{ battery,
     charging }`) and `pixels` (device pixels per face unit, for kit.js's `cached`).

   Read `tone` and `slot` through sceneTone() and sceneSlot(). The names and data the
   software relies on are listed in protocol/screen/vocabulary.json.
*/

import { TONES } from '../tones.js';
import boot from './boot.js';
import charging from './charging.js';
import clock from './clock.js';
import reset from './reset.js';
import setup from './setup.js';
import timer from './timer.js';
import weather from './weather.js';

export const SCENES = { boot, charging, clock, reset, setup, timer, weather };

/** The tone of scene `spec` showing `data`; cyan if it names no known tone. */
export function sceneTone(spec, data) {
  const tone = typeof spec.tone === 'function' ? spec.tone(data) : spec.tone;
  return Object.hasOwn(TONES, tone) ? tone : 'cyan';
}

/** Where the face waits `t` seconds into scene `spec`. */
export function sceneSlot(spec, t) {
  return typeof spec.slot === 'function' ? spec.slot(t) : spec.slot;
}
