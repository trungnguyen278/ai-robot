// SPDX-License-Identifier: MIT
/* Expressions, as data.

   Each expression gives:
   - `tone`: its colour (tones.js);
   - `face`, `eyes` (both eyes), `left`, `right`: the numbers that differ from the
     resting face (pose.js);
   - `motion`: its movement (motions.js), a name or `{ type, ...settings }`;
   - `effect`: what it draws around the eyes (effects.js);
   - `life`: how much of each "alive" layer it keeps, as a factor (1: normal, 0: none):
     `blink`, `glance` (looking around), `breathe`; and `voice`, how it reacts to a
     voice: "listen" (the eyes swell) or "talk" (the mouth moves).

   Shapes and colours follow the previous generation's design (ui/legacy/). The names
   required by the screen protocol are listed in protocol/screen/vocabulary.json; the
   page shows `asleep` on its own, while the robot sleeps (sleep.js).
*/

export const EXPRESSIONS = {
  neutral: {
    tone: 'cyan',
    life: { blink: 1, glance: 1, breathe: 1 },
  },

  listening: {
    tone: 'cyan',
    eyes: { w: 92, h: 101, pupil: 12 },
    motion: 'pulse',
    effect: 'bars',
    life: { blink: 0.6, glance: 0.2, breathe: 0.6, voice: 'listen' },
  },

  thinking: {
    tone: 'cyan',
    face: { y: -4 },
    eyes: { pupil: 12, pupilX: -10, pupilY: -16, lidBottom: 0.08 },
    motion: 'ponder',
    effect: 'dots',
    life: { blink: 0.5, glance: 0, breathe: 0.6 },
  },

  speaking: {
    tone: 'cyan',
    face: { mouth: 0.9, mouthW: 30, mouthCurve: 3 },
    eyes: { h: 90 },
    life: { blink: 1, glance: 0.6, breathe: 0.5, voice: 'talk' },
  },

  happy: {
    tone: 'warm',
    face: { y: -4 },
    eyes: { h: 92, r: 44, lidBottom: 0.5, lidBottomBend: 0.6 },
    motion: { type: 'bounce', period: 0.9, height: 4 },
    life: { blink: 0, glance: 0.4, breathe: 0.5 },
  },

  sad: {
    tone: 'blue',
    face: { y: 6, mouth: 0.7, mouthCurve: -5 },
    eyes: { h: 75, lidTop: 0.16, lidTopAngle: -22 },
    motion: 'droop',
    effect: 'tear',
    life: { blink: 0.7, glance: 0.3, breathe: 0.8 },
  },

  angry: {
    tone: 'red',
    eyes: { h: 60, r: 12, lidTopAngle: 18, brow: 1, browY: -48, browAngle: 17 },
    motion: { type: 'shake', freq: 2.2, amp: 0.8 },
    life: { blink: 0.5, glance: 0, breathe: 0.4 },
  },

  surprised: {
    tone: 'white',
    face: { y: -4, mouth: 0.85, mouthOpen: 1 },
    eyes: { w: 100, h: 104, r: 52 },
    motion: 'pop',
    life: { blink: 0.3, glance: 0, breathe: 0.4 },
  },

  sleepy: {
    tone: 'purple',
    face: { y: 8 },
    eyes: { y: -14, r: 14, lidTop: 0.72 },
    motion: 'drift',
    effect: 'zzz',
    life: { blink: 0.3, glance: 0, breathe: 1.4 },
  },

  confused: {
    tone: 'orange',
    face: { tilt: -6, mouth: 0.6, mouthW: 26, mouthCurve: -2 },
    left: { h: 84, lidTop: 0.08, lidTopAngle: -12 },
    right: { h: 64, lidTop: 0.06, lidTopAngle: 14 },
    motion: 'wobble',
    effect: 'question',
    life: { blink: 1, glance: 0.5, breathe: 0.6 },
  },

  curious: {
    tone: 'orange',
    face: { x: 4, tilt: 7 },
    left: { w: 94, h: 102 },
    right: { w: 82, h: 86, lidTop: 0.06 },
    motion: 'sway',
    life: { blink: 0.8, glance: 1.4, breathe: 0.6 },
  },

  love: {
    tone: 'rose',
    eyes: { w: 90, r: 44, lidBottom: 0.45, lidBottomBend: 0.5 },
    motion: 'beat',
    effect: 'hearts',
    life: { blink: 0.4, glance: 0.3, breathe: 0.8 },
  },

  excited: {
    tone: 'warm',
    eyes: { w: 96, h: 104, r: 30 },
    motion: { type: 'bounce', period: 0.45, height: 9 },
    effect: 'sparkles',
    life: { blink: 0.6, glance: 0.6, breathe: 0 },
  },

  scared: {
    tone: 'white',
    face: { y: 4, mouth: 0.7, mouthW: 30, mouthCurve: -3 },
    eyes: { w: 70, h: 80, r: 24, pupil: 9 },
    motion: { type: 'shake', freq: 7, amp: 1.4 },
    effect: 'sweat',
    life: { blink: 1.6, glance: 1.8, breathe: 0 },
  },

  // Not one the software asks for: the page's own, while the robot sleeps.
  asleep: {
    tone: 'purple',
    face: { y: 8 },
    eyes: { y: 28, h: 10, r: 5 }, // shut, where sleepy's lids meet
    motion: 'drift',
    effect: 'zzz',
    life: { blink: 0, glance: 0, breathe: 1.6 },
  },
};

export const DEFAULT_EXPRESSION = 'neutral';
