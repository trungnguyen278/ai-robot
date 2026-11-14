// SPDX-License-Identifier: MIT
/* The renderer's settings: how the face changes, stays alive and responds, in one
   place, so that tuning it means editing this file only.

   Not here:
   - the face's shapes, motions and colours, and the scenes' drawings: they are content,
     in expressions.js, motions.js, tones.js and scenes/;
   - what the screen protocol fixes (link.js, and the voice's half second of silence in
     animator.js);
   - what the robot decides, such as when the battery is low: the software sends it. */

export const SETTINGS = Object.freeze({
  transition: 0.28, // seconds for an expression to blend into the next
  sceneTransition: 0.5, // seconds for a scene to fade in or out

  blink: Object.freeze({
    close: 0.06, // seconds to close the eyes
    open: 0.1, // seconds to open them again
    first: [1.5, 4], // seconds before the first blink, at random within this range
    between: [2.2, 5.8], // seconds between blinks, divided by the expression's blink rate
    slowest: 0.2, // the lowest blink rate that division uses
    twice: 0.18, // the chance that a blink is followed by a second one
    gap: 0.12, // seconds between the two blinks of a pair
    onChange: 0.4, // the chance that a blink hides a change of expression
    shut: 3, // the height of a closed eye, in face units
  }),

  breathe: Object.freeze({
    period: 4.4, // seconds per breath
    rise: 1.6, // face units the face rises and sinks
    swell: 0.012, // how much the eyes grow with a breath, as a fraction
  }),

  glance: Object.freeze({
    every: [1.4, 4], // seconds between glances, at random within this range
    centre: 0.35, // the chance that a glance goes back to the centre
    reach: Object.freeze({ x: 10, y: 5 }), // face units a glance goes, at most, each way
    speed: 0.06, // seconds to cover about 63 % of the way
    pupils: 0.6, // how far the pupils follow a glance, as a fraction
  }),

  gaze: Object.freeze({
    speed: 0.1, // seconds to cover about 63 % of the way to a new target
    face: Object.freeze({ x: 26, y: 16 }), // face units the face turns at the screen's edge
    pupils: Object.freeze({ x: 10, y: 8 }), // face units the pupils lead by at the edge
    nearer: 0.06, // how much the eye nearer the target grows at the edge, as a fraction
  }),

  voice: Object.freeze({
    rise: 0.04, // seconds for the level to cover about 63 % on the way up
    fall: 0.16, // and on the way down
    listen: 0.1, // how much the eyes swell at full voice while listening, as a fraction
    talk: 0.06, // how much they narrow at full voice while talking, as a fraction
    mouth: 0.8, // how far the mouth opens at full voice
  }),

  hold: Object.freeze({
    seconds: 3, // a press held this long, still, opens the settings
    slop: 16, // CSS pixels a held press may wander, as the finger rolls, and still count
  }),

  // Falling asleep, once the software says the robot sleeps (sleep.js).
  sleep: Object.freeze({
    drowsy: 3, // seconds of a sleepy face before the eyes close
    release: 2, // seconds of closed eyes before the screen may turn off
  }),

  panel: Object.freeze({
    idle: 60, // seconds without a touch before a panel, such as the settings, closes itself
  }),
});
