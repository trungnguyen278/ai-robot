// SPDX-License-Identifier: MIT
/* Tests of the renderer (how to run them: ui/README.md).

   They check the expressions and scenes against the screen protocol's vocabulary,
   draw every expression and every scene (with good, missing and malformed data) on
   every screen profile, and check the animator: the same calls give the same frames,
   transitions never jump, scenes move the face smoothly and keep it still between
   two scenes that share a corner, and protocol messages land where they should. They
   work panels as an owner would, and check holds and the status row. The page also
   shows every expression and scene, for a look. The verdict goes to
   #result as data-status="pass" or data-status="fail", with one line per failure.
*/

import { Animator } from '../src/animator.js';
import { SETTINGS } from '../src/settings.js';
import { drawFrame } from '../src/draw.js';
import { EFFECTS } from '../src/effects.js';
import { DEFAULT_EXPRESSION, EXPRESSIONS } from '../src/expressions.js';
import { PROFILES, fit } from '../src/layout.js';
import { Link, PROTOCOL, apply } from '../src/link.js';
import { MOTIONS } from '../src/motions.js';
import { EXAMPLE, KINDS, PanelView, TARGET } from '../src/panel.js';
import { EYE, FACE } from '../src/pose.js';
import { Press } from '../src/press.js';
import { SCENES, sceneSlot, sceneTone } from '../src/scenes/index.js';
import { ASLEEP, SLEEPY, Sleep } from '../src/sleep.js';
import { CORNER } from '../src/scenes/kit.js';
import { CODE_LENGTH, DISCRIMINATOR_LENGTH, LINK_LENGTH, STATES } from '../src/scenes/setup.js';
import { LABEL_LENGTH } from '../src/scenes/timer.js';
import { CONDITIONS, PLACE_LENGTH, UNITS } from '../src/scenes/weather.js';
import { clip } from '../src/text.js';
import { TONES } from '../src/tones.js';

const VOCABULARY = '../../protocol/screen/vocabulary.json';
const MOMENTS = [0, 0.37, 1.9, 4.4];
const LIFE_KEYS = ['blink', 'glance', 'breathe', 'voice'];
const STEP = 1 / 120; // seconds between two frames, in the motion checks

const failures = [];
const fail = (where, message) => failures.push(`${where}: ${message}`);
const check = (ok, where, message) => {
  if (!ok) fail(where, message);
};
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// Timers that run only when told to, for what waits: a panel's idle time, a hold.
function fakeTimers() {
  const pending = new Map();
  let last = 0;
  return {
    pending,
    set: (run, ms) => {
      last += 1;
      pending.set(last, { run, ms });
      return last;
    },
    clear: (id) => pending.delete(id),
    runAll() {
      const due = [...pending.values()];
      pending.clear();
      for (const { run } of due) run();
    },
  };
}

// An animator without blinks or glances, so that frames are easy to compare.
function still(expression = DEFAULT_EXPRESSION, seed = 3) {
  const animator = new Animator({ expression, seed });
  animator.enabled = { blink: false, glance: false, breathe: false };
  return animator;
}

async function checkVocabulary() {
  const vocabulary = await (await fetch(VOCABULARY)).json();
  check(vocabulary.protocol === PROTOCOL, 'protocol',
    `vocabulary.json is version ${vocabulary.protocol}, link.js speaks ${PROTOCOL}`);
  for (const name of Object.keys(vocabulary.expressions)) {
    check(Object.hasOwn(EXPRESSIONS, name), name, 'required by the protocol, missing from expressions.js');
  }
  check(same(Object.keys(TONES), vocabulary.tones), 'tones',
    `tones.js has ${Object.keys(TONES).join(', ')}; the protocol lists ${vocabulary.tones.join(', ')}`);
  for (const name of Object.keys(vocabulary.scenes)) {
    check(Object.hasOwn(SCENES, name), name, 'required by the protocol, missing from scenes/');
  }
  // What the scenes accept, against what the protocol says they take.
  const { weather, timer, setup } = vocabulary.scenes;
  check(same(Object.keys(CONDITIONS), weather.data.condition.values), 'weather',
    `draws ${Object.keys(CONDITIONS).join(', ')}; the protocol lists ${weather.data.condition.values.join(', ')}`);
  check(same(UNITS, weather.data.unit.values), 'weather', 'units differ from the protocol');
  check(PLACE_LENGTH === weather.data.place.max_length, 'weather', 'place length differs from the protocol');
  check(LABEL_LENGTH === timer.data.label.max_length, 'timer', 'label length differs from the protocol');
  check(same(STATES, setup.data.state.values), 'setup',
    `draws ${STATES.join(', ')}; the protocol lists ${setup.data.state.values.join(', ')}`);
  check(CODE_LENGTH === setup.data.code.max_length, 'setup', 'code length differs from the protocol');
  check(DISCRIMINATOR_LENGTH === setup.data.discriminator.max_length, 'setup',
    'discriminator length differs from the protocol');
  check(LINK_LENGTH === setup.data.link.max_length, 'setup', 'link length differs from the protocol');
  check(same(KINDS, Object.keys(vocabulary.panel_items)), 'panel',
    `draws ${KINDS.join(', ')}; the protocol lists ${Object.keys(vocabulary.panel_items).join(', ')}`);
}

function checkExpressions() {
  check(Object.hasOwn(EXPRESSIONS, DEFAULT_EXPRESSION), DEFAULT_EXPRESSION, 'the default expression is missing');
  for (const [name, spec] of Object.entries(EXPRESSIONS)) {
    check(Boolean(TONES[spec.tone]), name, `unknown tone ${spec.tone}`);
    for (const [part, model] of [['face', FACE], ['eyes', EYE], ['left', EYE], ['right', EYE]]) {
      for (const [key, value] of Object.entries(spec[part] ?? {})) {
        check(Object.hasOwn(model, key), name, `${part}.${key} is not part of the face model`);
        check(Number.isFinite(value), name, `${part}.${key} is not a number`);
      }
    }
    if (spec.motion) {
      const type = typeof spec.motion === 'string' ? spec.motion : spec.motion.type;
      check(Boolean(MOTIONS[type]), name, `unknown motion ${type}`);
    }
    if (spec.effect) check(Boolean(EFFECTS[spec.effect]), name, `unknown effect ${spec.effect}`);
    for (const key of Object.keys(spec.life ?? {})) check(LIFE_KEYS.includes(key), name, `unknown life layer ${key}`);
    if (spec.life?.voice) check(['listen', 'talk'].includes(spec.life.voice), name, `unknown voice reaction ${spec.life.voice}`);
  }
}

const slotOk = (slot) => Boolean(slot) && ['x', 'y', 'scale'].every((key) => Number.isFinite(slot[key]));

function checkScenes() {
  for (const [name, spec] of Object.entries(SCENES)) {
    check(typeof spec.draw === 'function', name, 'no draw function');
    check(spec.example && typeof spec.example === 'object', name, 'no example data');
    check(Object.hasOwn(TONES, sceneTone(spec, spec.example)), name, 'no known tone');
    for (const t of MOMENTS) check(slotOk(sceneSlot(spec, t)), name, `no valid face slot at ${t} s`);
  }
  // The weather's colour and its sky agree on what counts as night.
  const weather = SCENES.weather;
  for (const night of [1, 'true', 'yes']) {
    check(sceneTone(weather, { condition: 'clear', night }) === sceneTone(weather, { condition: 'clear' }),
      'weather', `night: ${JSON.stringify(night)} must count as day, as it does for the sky`);
  }
  check(sceneTone({ tone: () => 'no-such-tone' }, {}) === 'cyan', 'scenes', 'an unknown tone must read as cyan');
}

function litPixels(ctx, width, height) {
  const { data } = ctx.getImageData(0, 0, width, height);
  let lit = 0;
  for (let i = 0; i < data.length; i += 4) if (Math.max(data[i], data[i + 1], data[i + 2]) > 80) lit += 1;
  return lit;
}

// Draw `animator` at every moment, then check that something was drawn.
function drawAll(where, ctx, layout, animator) {
  for (const t of MOMENTS) {
    try {
      drawFrame(ctx, layout, animator.frame(t), { status: { battery: 12, charging: true } });
    } catch (error) {
      fail(`${where} at ${t} s`, error.message);
    }
  }
  check(litPixels(ctx, layout.width, layout.height) > layout.width * layout.height * 0.01, where, 'almost nothing drawn');
}

// Data that is wrong in every way a scene could trip on.
const NONSENSE = {
  condition: 'volcano', temperature: 'hot', unit: 7, night: 'yes', place: 42,
  ends_at: null, duration: -5, label: {},
  state: 'flying', code: 12345678, discriminator: [], link: { url: 'x' },
};

function checkDrawing() {
  for (const profile of PROFILES) {
    const layout = fit(profile.width, profile.height, profile.shape);
    const ctx = new OffscreenCanvas(profile.width, profile.height).getContext('2d');
    for (const name of Object.keys(EXPRESSIONS)) drawAll(`${name} on ${profile.id}`, ctx, layout, still(name));
    for (const [name, spec] of Object.entries(SCENES)) {
      for (const data of [{ ...spec.example, ends_at: Date.now() / 1000 + 30 }, {}, NONSENSE]) {
        const animator = still();
        animator.setScene(name, data, 0);
        drawAll(`scene ${name} on ${profile.id}`, ctx, layout, animator);
      }
    }
  }
}

function checkAnimator() {
  // The same calls give the same frames, glances and blinks included.
  const a = new Animator({ seed: 11 });
  const b = new Animator({ seed: 11 });
  for (let i = 0; i <= 600; i++) {
    const t = i / 60;
    if (i === 120) { a.set('happy', null, t); b.set('happy', null, t); }
    if (i === 300) { a.look({ x: 0.5, y: -0.3 }); b.look({ x: 0.5, y: -0.3 }); }
    if (!same(a.frame(t), b.frame(t))) {
      fail('animator', `two animators with the same seed differ at ${t.toFixed(2)} s`);
      break;
    }
  }

  // Transitions start where the face is and never jump, even when interrupted.
  const names = Object.keys(EXPRESSIONS);
  const animator = still();
  let previous = animator.frame(0).pose;
  let worst = { jump: 0, at: '' };
  for (let i = 1; i <= 1200; i++) {
    const t = i * STEP;
    if (i % 19 === 0) animator.set(names[(i / 19) % names.length], null, t); // faster than TRANSITION
    const { pose } = animator.frame(t);
    for (const part of ['face', 'left', 'right']) {
      for (const key of Object.keys(pose[part])) {
        const jump = Math.abs(pose[part][key] - previous[part][key]);
        if (jump > worst.jump) worst = { jump, at: `${part}.${key} at ${t.toFixed(3)} s` };
      }
    }
    previous = pose;
  }
  check(worst.jump < 6, 'transitions', `jump of ${worst.jump.toFixed(2)} in ${worst.at}`);

  // Once a transition is over, only the new expression is left.
  const settled = still('sad');
  settled.set('angry', null, 1);
  settled.frame(1 + SETTINGS.transition + 0.01);
  check(settled.layers.length === 1 && settled.expression === 'angry', 'transitions', 'the old expression lingers');

  check(new Animator().set('no-such-expression') === DEFAULT_EXPRESSION, 'animator',
    'an unknown expression must show the default one');
}

// Frames of `animator` from `from` to `to` seconds; `act(t)` runs before each.
function frames(animator, from, to, act = () => {}) {
  const out = [];
  for (let t = from; t <= to + 1e-9; t += STEP) {
    act(t);
    out.push(animator.frame(t));
  }
  return out;
}

// The largest change between two frames of `value(frame)`.
const largestStep = (list, value) => list.slice(1).reduce((m, f, i) => Math.max(m, Math.abs(value(f) - value(list[i]))), 0);

function checkSceneMotion() {
  // Switching between two scenes that share the corner keeps the face there.
  const corner = still();
  corner.setScene('clock', {}, 0);
  let drift = 0;
  for (const { slot } of frames(corner, 1, 1.6, (t) => { if (t === 1) corner.setScene('weather', {}, t); })) {
    drift = Math.max(drift, Math.hypot(slot.x - CORNER.x, slot.y - CORNER.y), 100 * Math.abs(slot.scale - CORNER.scale));
  }
  check(drift < 0.5, 'scenes', `the face left its corner by ${drift.toFixed(2)} while the scene changed`);

  // Scenes that come and go faster than they fade never make the face jump.
  const names = [null, ...Object.keys(SCENES)];
  const busy = still();
  let i = 0;
  const busyFrames = frames(busy, 0, 10, () => { if (++i % 23 === 0) busy.setScene(names[(i / 23) % names.length], {}, i * STEP); });
  const jump = Math.max(
    largestStep(busyFrames, (f) => f.slot.x), largestStep(busyFrames, (f) => f.slot.y),
    100 * largestStep(busyFrames, (f) => f.slot.scale));
  check(jump < 6, 'scenes', `the face jumped by ${jump.toFixed(2)} between two frames`);
  check(busy.setScene('no-such-scene', {}, 11) === null, 'scenes', 'an unknown scene must show none');

  // A scene asked for again while it fades out fades back in from where it was.
  const back = still();
  back.setScene('clock', {}, 0);
  const backFrames = frames(back, 1, 2, (t) => {
    if (Math.abs(t - 1) < 1e-9) back.setScene(null, {}, t);
    if (Math.abs(t - 1.2) < STEP / 2) back.setScene('clock', {}, t);
  });
  const alpha = (f) => f.scenes.reduce((sum, s) => sum + s.alpha, 0);
  check(largestStep(backFrames, alpha) < 0.05 && Math.abs(alpha(backFrames.at(-1)) - 1) < 1e-6
    && back.sceneLayers.length === 1, 'scenes', 'a scene asked for again must fade back in smoothly');

  // `restart` plays a scene from its start.
  back.setScene('clock', {}, 3, { restart: true });
  check(back.sceneLayers.at(-1).start === 3, 'scenes', 'restart must start the scene again');

  // New data that changes the colour fades it in; new data that does not, does not.
  const sky = still();
  sky.setScene('weather', { condition: 'clear' }, 0);
  sky.setScene('weather', { condition: 'clear', temperature: 20 }, 1);
  check(sky.sceneLayers.length === 1, 'scenes', 'new data must update the scene in place');
  sky.setScene('weather', { condition: 'rain' }, 2);
  const crossfade = sky.frame(2.2);
  check(crossfade.scenes.length === 2 && Math.abs(crossfade.slot.x - CORNER.x) < 1e-6, 'scenes',
    'a new colour must fade in over the old one, with the face kept in its corner');

  // A held scene plays to its end; what was asked for meanwhile follows.
  const wake = still();
  wake.setScene('boot', {}, 0, { instant: true, hold: SCENES.boot.length });
  wake.setScene(null, {}, 0.1);
  wake.setScene('clock', {}, 0.2);
  wake.frame(0.6);
  check(wake.scene === 'boot', 'scenes', 'a held scene must stay until its end');
  wake.frame(SCENES.boot.length + 0.05);
  check(wake.scene === 'clock', 'scenes', 'the scene asked for during a hold must follow it');

  // The status row's time fades with the clock scene instead of blinking out.
  const fade = still();
  const fadeFrames = frames(fade, 0, 1, (t) => { if (t === 0) fade.setScene('clock', {}, t); });
  check(largestStep(fadeFrames, (f) => f.clockAlpha) < 0.05 && fadeFrames.at(-1).clockAlpha === 0, 'status',
    'the status row\'s time must fade out smoothly while the clock comes in');

  // When a scene leaves, the face goes back to the middle.
  const leaving = still();
  leaving.setScene('clock', {}, 0);
  leaving.setScene(null, {}, 1);
  const after = leaving.frame(1 + SETTINGS.sceneTransition + 0.01);
  check(after.scenes.length === 0 && after.slot.scale === 1, 'scenes', 'the face must be back in the middle');
}

// What a scene writes, with the font and position of each piece of text.
function written(name, data, now = new Date()) {
  const ctx = new OffscreenCanvas(320, 240).getContext('2d');
  const texts = [];
  const fillText = ctx.fillText.bind(ctx);
  ctx.fillText = (value, x, y) => {
    texts.push({ value, x, font: ctx.font, align: ctx.textAlign });
    fillText(value, x, y);
  };
  SCENES[name].draw(ctx, 1, { alpha: 1, data, now, status: {}, pixels: 1 });
  return { ctx, texts };
}

function checkSceneText() {
  const now = new Date();
  const joined = (name, data) => written(name, data, now).texts.map((t) => t.value).join('');
  check(joined('timer', {}).includes('--:--'), 'timer', 'without ends_at it must show --:--');
  check(joined('timer', { ends_at: now.getTime() / 1000 + 65, duration: 120 }).includes('1:05'), 'timer',
    'the time left is wrong');
  check(joined('timer', { ends_at: now.getTime() / 1000 - 1, duration: 60 }).includes('Done'), 'timer',
    'at zero it must say Done');

  // The temperature shrinks to fit its column instead of running off the screen.
  for (const [temperature, unit] of [[104, 'F'], [-12, 'C'], [-99, 'C'], [199, 'F']]) {
    const { ctx, texts } = written('weather', { condition: 'rain', temperature, unit }, now);
    const last = texts.find((t) => t.value === `°${unit}`);
    ctx.font = last.font;
    const right = last.x + ctx.measureText(last.value).width;
    check(right <= 151, 'weather', `${temperature}°${unit} runs to x=${right.toFixed(1)}`);
  }

  const tea = 'Tea time 🍵🍵🍵🍵';
  check(clip(tea, 12) === 'Tea time 🍵🍵🍵' && !/[\uD800-\uDBFF]$/.test(clip(tea, 11)), 'text',
    'clip must count characters, not UTF-16 units');
}

function checkProtocol() {
  const player = { animator: still(), status: {}, offset: 0, now: () => 2 };
  apply({ type: 'state', face: { expression: 'sad', tone: 'red' }, status: { battery: 40, charging: true } }, player);
  const top = player.animator.layers.at(-1);
  check(top.name === 'sad' && top.tone === 'red', 'state', `shows ${top.name} in ${top.tone}`);
  check(player.status.battery === 40 && player.status.charging === true, 'state', 'status not applied');
  check(player.status.low === null, 'state', 'a missing low-battery flag must read as unknown');
  apply({ type: 'state', face: {}, status: { battery: 9, charging: false, low: true } }, player);
  check(player.status.low === true, 'state', 'the robot\'s low-battery flag not applied');
  apply({ type: 'state', face: { expression: 42 }, status: { battery: 'full', low: 'yes' } }, player);
  check(player.status.low === null, 'state', 'a malformed low-battery flag must read as unknown');
  check(player.animator.expression === DEFAULT_EXPRESSION, 'state', 'a malformed expression must show the default one');
  check(player.status.battery === null, 'state', 'a malformed battery level must read as unknown');
  apply({ type: 'look', target: { x: 2, y: -0.5 } }, player);
  check(player.animator.gaze.target?.x === 1 && player.animator.gaze.target?.y === -0.5, 'look', 'target not clamped to [-1, 1]');
  apply({ type: 'look', target: null }, player);
  check(player.animator.gaze.target === null, 'look', 'null must release the gaze');
  apply({ type: 'voice', level: 0.5 }, player);
  check(player.animator.heard.target === 0.5, 'voice', 'level not applied');
  check(apply({ type: 'from-the-future' }, player) === false, 'messages', 'unknown types must be ignored');

  apply({ type: 'state', face: {}, scene: { name: 'weather', data: { condition: 'rain', temperature: 18 } } }, player);
  check(player.animator.scene === 'weather', 'state', 'scene not shown');
  apply({ type: 'state', face: {}, scene: { name: 'weather', data: { condition: 'rain', temperature: 12 } } }, player);
  const layers = player.animator.sceneLayers;
  check(layers.length === 1 && layers[0].data.temperature === 12, 'state',
    'new data must update the scene without starting it again');
  apply({ type: 'state', face: {}, scene: null }, player);
  check(player.animator.scene === null, 'state', 'a null scene must take the scene away');
  apply({ type: 'state', face: {}, scene: { name: 'volcano' } }, player);
  check(player.animator.scene === null, 'state', 'an unknown scene must show none');

  // The microphones, in the status row, and panels.
  apply({ type: 'state', face: {}, status: { muted: true } }, player);
  check(player.status.muted === true, 'state', 'the microphones flag not applied');
  apply({ type: 'state', face: {}, status: { muted: 'off' } }, player);
  check(player.status.muted === null, 'state', 'a malformed microphones flag must read as unknown');
  const shown = [];
  const panels = { show: (panel) => shown.push(panel) };
  apply({ type: 'state', face: {}, panel: EXAMPLE }, player, panels);
  apply({ type: 'state', face: {} }, player, panels);
  check(shown[0] === EXAMPLE && shown[1] === null, 'state', 'a panel must show, and a missing one go');

  // Times from the software are read on its clock.
  apply({ type: 'hello', protocol: PROTOCOL, time: Date.now() / 1000 + 30 }, player);
  check(Math.abs(player.offset - 30) < 1, 'hello', `the software's clock is off by ${player.offset.toFixed(2)} s, not 30`);

  // A page out of date reloads itself.
  const reloads = [];
  const link = new Link(player, { url: 'ws://unused', reload: () => reloads.push(true) });
  link.hello({ protocol: PROTOCOL, revision: 'a' });
  link.hello({ protocol: PROTOCOL, revision: 'a' });
  check(reloads.length === 0, 'hello', 'the same revision must not reload the page');
  link.hello({ protocol: PROTOCOL, revision: 'b' });
  check(reloads.length === 1, 'hello', 'a new revision must reload the page');

  // What the software sends reaches the face, and the robot's sleep.
  const followed = [];
  const sleep = {
    follow: (asleep) => {
      followed.push(asleep);
      return false; // the face is the software's
    },
  };
  const awake = { animator: still(), status: {}, offset: 0, now: () => 2 };
  const reloaded = [];
  const linked = new Link(awake, { url: 'ws://unused', sleep, reload: () => reloaded.push(true) });
  linked.receive('not JSON');
  linked.receive(JSON.stringify({ type: 'state', face: { expression: 'sad' }, asleep: false }));
  check(same(followed, [false]) && awake.animator.layers.at(-1).name === 'sad', 'link',
    'a state must reach the face and the sleep, and text that is not JSON nothing');
  linked.receive(JSON.stringify({ type: 'hello', protocol: PROTOCOL, revision: 'a' }));
  linked.receive(JSON.stringify({ type: 'hello', protocol: PROTOCOL, revision: 'b' }));
  check(reloaded.length === 1, 'link', 'a greeting received must be checked');

  // What the renderer reports: a button's action has no value.
  const sent = [];
  link.socket = { readyState: WebSocket.OPEN, send: (text) => sent.push(JSON.parse(text)) };
  link.hold(0.5, 0.25);
  link.action('settings', 'voice.volume', 60);
  link.action('settings', 'setup.start');
  link.close('settings');
  check(same(sent, [
    { type: 'hold', x: 0.5, y: 0.25 },
    { type: 'action', panel: 'settings', item: 'voice.volume', value: 60 },
    { type: 'action', panel: 'settings', item: 'setup.start' },
    { type: 'close', panel: 'settings' },
  ]), 'link', `sent ${JSON.stringify(sent)}`);
}

// A panel with every kind of item, and items and sections a renderer must leave out.
const EVERY_KIND = {
  name: 'test',
  title: 'Test',
  sections: [
    { title: 'One', items: [
      { id: 'a.toggle', kind: 'toggle', label: 'Toggle', value: false, confirm: 'Sure?' },
      { id: 'a.slider', kind: 'slider', label: 'Slider', value: 40, min: 0, max: 100, step: 10, unit: '%' },
      { id: 'a.choice', kind: 'choice', label: 'Unit', value: 'C',
        choices: [{ value: 'C', label: '°C' }, { value: 'F', label: '°F' }, 'not a choice'] },
      { id: 'a.button', kind: 'button', label: 'Button', confirm: 'Really?' },
      { id: 'a.info', kind: 'info', label: 'Info', value: 'text' },
      { id: 'a.info', kind: 'info', label: 'The same id', value: 'again' },
      { id: 'a.dial', kind: 'dial', label: 'An unknown kind' },
      { kind: 'info', label: 'No id' },
      null,
    ] },
    { title: 'Nothing to draw', items: [{ id: 'b.gauge', kind: 'gauge' }] },
    'not a section',
  ],
};

// `panel` with the item `id` given `value`, as the software would send it.
const withValue = (panel, id, value) => ({
  ...panel,
  sections: panel.sections.map((section) => (Array.isArray(section?.items)
    ? { ...section, items: section.items.map((item) => (item?.id === id ? { ...item, value } : item)) }
    : section)),
});

// Work a panel as an owner would.
function checkPanel() {
  const root = document.createElement('div');
  document.body.append(root);
  const timers = fakeTimers();
  const actions = [];
  const closed = [];
  const view = new PanelView(root, {
    idle: 60,
    timers,
    onAction: (...action) => actions.push(action),
    onClose: (name) => closed.push(name),
  });
  const $ = (selector) => root.querySelector(selector);
  const $$ = (selector) => [...root.querySelectorAll(selector)];
  const checked = (element) => element?.getAttribute('aria-checked');
  try {
    check(root.hidden, 'panel', 'must be hidden until there is a panel');
    view.show(EVERY_KIND);
    check(!root.hidden && $('h1')?.textContent === 'Test', 'panel', 'not shown with its title');
    const kinds = $$('.item').map((row) => row.classList[1]);
    check(same(kinds, KINDS), 'panel', `draws ${kinds.join(', ')}; each kind once, malformed items left out`);
    check($$('section').length === 1, 'panel', 'a section with nothing to draw must be left out');
    check($$('.choices button').length === 2, 'panel', 'a malformed choice must be left out');
    const small = $$('button, input').filter((target) => target.getBoundingClientRect().height < TARGET);
    check(small.length === 0, 'panel', `targets under ${TARGET} px high: ${small.map((t) => t.className || t.type).join(', ')}`);

    // A toggle that asks before it is switched on.
    const toggle = $('.toggle .switch');
    toggle.click();
    check(actions.length === 0 && !$('.question').hidden && $('.question p')?.textContent === 'Sure?', 'toggle',
      'must ask before it is switched on');
    $('.question .no').click();
    check(actions.length === 0 && checked(toggle) === 'false' && $('.question').hidden, 'toggle',
      'Cancel must leave it off');
    toggle.click();
    $('.question .yes').click();
    check(same(actions.pop(), ['test', 'a.toggle', true]) && checked(toggle) === 'true', 'toggle',
      'Yes must switch it on, and say so');
    toggle.click();
    check(same(actions.pop(), ['test', 'a.toggle', false]) && $('.question').hidden, 'toggle',
      'switching off must not ask');
    // The robot refuses: the panel, sent again as it was, puts the value back, in place.
    toggle.click();
    $('.question .yes').click();
    actions.length = 0;
    view.show(EVERY_KIND);
    check($('.toggle .switch') === toggle && checked(toggle) === 'false', 'toggle',
      'the panel sent again must update in place, and put back a refused value');

    // A slider sends its value when released, and stays under the finger.
    const slider = $('.slider input');
    const output = $('.slider output');
    check(slider.value === '40' && output.textContent === '40%', 'slider', `shows ${slider.value}, ${output.textContent}`);
    slider.value = '60';
    slider.dispatchEvent(new Event('input'));
    check(output.textContent === '60%' && actions.length === 0, 'slider',
      'must show its value as it moves, and send it only when released');
    slider.dispatchEvent(new Event('change'));
    check(same(actions.pop(), ['test', 'a.slider', 60]), 'slider', 'the value must be sent when released');
    slider.dispatchEvent(new PointerEvent('pointerdown'));
    view.show(withValue(EVERY_KIND, 'a.slider', 20));
    check(slider.value === '60', 'slider', 'must stay under the finger');
    slider.dispatchEvent(new PointerEvent('pointerup'));
    check(slider.value === '20' && output.textContent === '20%', 'slider',
      "a finger lifted without moving it must leave the software's value");

    // A choice.
    const [celsius, fahrenheit] = $$('.choices button');
    check(checked(celsius) === 'true' && checked(fahrenheit) === 'false', 'choice', 'the value is not the one marked');
    celsius.click();
    check(actions.length === 0, 'choice', 'choosing what is chosen must send nothing');
    fahrenheit.click();
    check(same(actions.pop(), ['test', 'a.choice', 'F']) && checked(fahrenheit) === 'true' && checked(celsius) === 'false',
      'choice', 'a choice must show, and be sent');

    // A button that asks before it acts.
    $('.item.button').click();
    check(actions.length === 0 && $('.question p')?.textContent === 'Really?', 'button', 'must ask before it acts');
    $('.question .yes').click();
    const pressed = actions.pop();
    check(pressed?.length === 3 && pressed[1] === 'a.button' && pressed[2] === undefined, 'button',
      'must act without a value');

    // An info line, updated in place.
    const info = $('.info .value');
    view.show(withValue(EVERY_KIND, 'a.info', 'new text'));
    check($('.info .value') === info && info.textContent === 'new text', 'info', 'must update in place');

    // Escape takes a question away first, then the panel.
    $('.item.button').click();
    root.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    check($('.question').hidden && !root.hidden && actions.length === 0, 'panel', 'Escape must take a question away first');

    // Only the owner's touches keep a panel open.
    const [armed] = timers.pending.keys();
    check(timers.pending.size === 1 && timers.pending.get(armed).ms === 60000, 'panel', 'must close after its idle time');
    view.show(withValue(EVERY_KIND, 'a.info', 'later'));
    check(timers.pending.has(armed), 'panel', "the software's updates must not keep the panel open");
    root.dispatchEvent(new PointerEvent('pointerdown'));
    check(!timers.pending.has(armed) && timers.pending.size === 1, 'panel', 'a touch must keep it open longer');
    timers.runAll();
    check(same(closed, ['test']) && root.hidden && !root.firstChild, 'panel', 'left untouched, it must close, and say so');
    view.show(EVERY_KIND);
    $('.done').click();
    check(same(closed, ['test', 'test']) && root.hidden, 'panel', 'Done must close it, and say so');
    view.show(EVERY_KIND);
    view.show(null);
    check(closed.length === 2 && root.hidden && timers.pending.size === 0, 'panel',
      'a state without the panel must take it away without a word');
    for (const panel of [{ name: 'x' }, { name: '', sections: [] }, { sections: [] }, 'settings', 42]) {
      view.show(panel);
      check(root.hidden, 'panel', `${JSON.stringify(panel)} must show nothing`);
    }

    // A long panel keeps its place when it is updated, and when it gains an item.
    const lines = Array.from({ length: 40 }, (_, i) => ({ id: `l.${i}`, kind: 'info', label: `Line ${i}`, value: `${i}` }));
    const long = { name: 'long', title: 'Long', sections: [{ title: 'Lines', items: lines }] };
    view.show(long);
    const body = $('.body');
    body.scrollTop = 200;
    view.show(withValue(long, 'l.3', 'three'));
    check(body.scrollTop === 200, 'panel', 'an update must keep the place');
    view.show({ ...long, sections: [{ title: 'Lines', items: [...lines, { id: 'l.new', kind: 'info', label: 'New' }] }] });
    check($('.body') !== body && $('.body').scrollTop === 200, 'panel', 'a panel drawn anew must keep the place');
    view.show(EXAMPLE);
    check($('.body').scrollTop === 0, 'panel', 'another panel must start at its top');
    view.show(null);

    view.setShape('round');
    check(root.classList.contains('round'), 'panel', 'a round screen must keep the panel inside its circle');
  } finally {
    root.remove();
  }

  // With the browser's own timers, as on the robot.
  const own = document.createElement('div');
  document.body.append(own);
  try {
    const real = new PanelView(own);
    real.show(EXAMPLE);
    real.awake();
    real.close();
  } catch (error) {
    fail('panel', `with the browser's timers: ${error.message}`);
  } finally {
    own.remove();
  }
}

// A press held still is a hold; one that moves or ends is not.
// A wake lock that grants or refuses as told, and a page that shows or hides.
function fakeWakeLock() {
  const lock = {
    requests: 0,
    refuse: false,
    held: [], // the locks granted and not yet let go
    request(type) {
      this.requests += 1;
      if (type !== 'screen' || this.refuse) return Promise.reject(new Error('refused'));
      const granted = new EventTarget();
      granted.release = () => {
        lock.held = lock.held.filter((l) => l !== granted);
        granted.dispatchEvent(new Event('release'));
        return Promise.resolve();
      };
      lock.held.push(granted);
      return Promise.resolve(granted);
    },
  };
  return lock;
}

async function checkSleep() {
  const flush = async () => {
    for (let i = 0; i < 5; i += 1) await Promise.resolve();
  };
  const face = (player) => player.animator.layers.at(-1).name;

  // Without sleep (the studio), asleep still shows the sleeping face, and no scene or panel.
  const shown = [];
  const panels = { show: (panel) => shown.push(panel) };
  const bare = { animator: still(), status: {}, offset: 0, now: () => 2 };
  apply({ type: 'state', face: { expression: 'happy' }, scene: { name: 'clock' }, panel: EXAMPLE, asleep: true },
    bare, panels);
  check(face(bare) === ASLEEP && bare.animator.scene === null && shown[0] === null, 'sleep',
    'asleep, the face must sleep, with no scene or panel');
  apply({ type: 'state', face: { expression: 'happy' }, asleep: 'yes' }, bare);
  check(face(bare) === 'happy', 'sleep', 'only asleep: true puts the face to sleep');

  const timers = fakeTimers();
  const wakeLock = fakeWakeLock();
  const page = new EventTarget();
  page.visibilityState = 'visible';
  const player = { animator: still(), status: {}, offset: 0, now: () => 2 };
  const sleep = new Sleep(player, { drowsy: 3, release: 2, wakeLock, page, timers });
  const state = (asleep, expression = 'happy') => {
    apply({ type: 'state', face: { expression }, scene: { name: 'clock' }, asleep }, player, null, sleep);
  };

  state(false);
  state(false); // before the first request is answered
  await flush();
  check(wakeLock.requests === 1 && wakeLock.held.length === 1 && face(player) === 'happy', 'sleep',
    'awake, the page must hold one wake lock, asked for once');
  state(false, 'sad');
  await flush();
  check(wakeLock.requests === 1 && face(player) === 'sad', 'sleep', 'a held lock must not be asked for again');

  state(true);
  check(face(player) === SLEEPY && player.animator.scene === null, 'sleep', 'falling asleep, the face must grow sleepy');
  check(same([...timers.pending.values()].map((t) => t.ms), [3000, 5000]), 'sleep',
    'the eyes must close after drowsy, and the lock go after drowsy + release');
  state(true, 'love');
  check(timers.pending.size === 2 && face(player) === SLEEPY, 'sleep', 'a state while asleep must not start again');
  timers.runAll();
  check(face(player) === ASLEEP && wakeLock.held.length === 0, 'sleep', 'asleep, the eyes close and the lock goes');
  page.dispatchEvent(new Event('visibilitychange'));
  await flush();
  check(wakeLock.requests === 1, 'sleep', 'asleep, a page that shows again must not ask for the lock');

  state(false, 'neutral');
  await flush();
  check(face(player) === 'neutral' && wakeLock.held.length === 1, 'sleep', 'woken, the face and the lock come back');

  // Woken before the lock went: it is still held, and nothing is left to run.
  state(true);
  state(false);
  await flush();
  check(timers.pending.size === 0 && wakeLock.requests === 2 && wakeLock.held.length === 1, 'sleep',
    'woken while drowsy, the lock must stay and the timers go');

  // The browser lets the lock go as the page hides, and refuses it until it shows.
  page.visibilityState = 'hidden';
  wakeLock.held[0].release();
  await flush();
  check(wakeLock.requests === 2, 'sleep', 'a hidden page must not ask for the lock');
  page.visibilityState = 'visible';
  page.dispatchEvent(new Event('visibilitychange'));
  await flush();
  check(wakeLock.held.length === 1, 'sleep', 'a page shown again must hold the lock again');

  // A lock the browser lets go while the page shows is asked for again at once; granted
  // after the robot fell asleep, it goes at once.
  const asked = wakeLock.requests;
  wakeLock.held[0].release();
  check(wakeLock.requests === asked + 1, 'sleep', 'a lock the browser lets go must be asked for again');
  state(true);
  await flush();
  check(wakeLock.held.length === 0, 'sleep', 'a lock granted while asleep must go');
  timers.runAll();

  // Refused, it is asked for again when the page shows again.
  wakeLock.refuse = true;
  state(false);
  await flush();
  wakeLock.refuse = false;
  page.dispatchEvent(new Event('visibilitychange'));
  await flush();
  check(wakeLock.held.length === 1, 'sleep', 'a refused lock must be asked for again');

  // Without the Wake Lock API, sleep still shows.
  const plain = { animator: still(), status: {}, offset: 0, now: () => 2 };
  const without = new Sleep(plain, { drowsy: 3, release: 2, wakeLock: null, page: null, timers: fakeTimers() });
  apply({ type: 'state', face: {}, asleep: true }, plain, null, without);
  check(face(plain) === SLEEPY, 'sleep', 'without a wake lock, the face must still fall asleep');
}

function checkPress() {
  const timers = fakeTimers();
  const holds = [];
  const press = new Press({ ...SETTINGS.hold, timers, onHold: (x, y) => holds.push([x, y]) });
  const { seconds, slop } = SETTINGS.hold;
  press.down(100, 100);
  press.move(100 + slop * 0.6, 100 + slop * 0.6);
  check([...timers.pending.values()][0]?.ms === seconds * 1000, 'hold', 'must wait the hold time');
  timers.runAll();
  check(same(holds, [[100, 100]]), 'hold', 'a press within the slop must count, where it began');
  press.down(100, 100);
  press.move(100, 100 + slop * 1.5);
  timers.runAll();
  check(holds.length === 1, 'hold', 'a press that moves must not count');
  press.down(100, 100);
  press.up();
  timers.runAll();
  check(holds.length === 1 && timers.pending.size === 0, 'hold', 'a press that ends must not count');
  try {
    const real = new Press({ ...SETTINGS.hold, onHold: () => {} }); // the browser's own timers
    real.down(100, 100);
    real.up();
  } catch (error) {
    fail('hold', `with the browser's timers: ${error.message}`);
  }
}

// The status row shows a struck microphone, in red, while the microphones are off.
function checkStatusRow() {
  for (const profile of PROFILES) {
    const layout = fit(profile.width, profile.height, profile.shape);
    const ctx = new OffscreenCanvas(profile.width, profile.height).getContext('2d');
    const frame = still().frame(1);
    const red = (muted) => {
      drawFrame(ctx, layout, frame, { status: { battery: 50, charging: false, muted }, now: new Date(2026, 0, 1, 12) });
      const { data } = ctx.getImageData(0, 0, profile.width, profile.height);
      let count = 0;
      for (let i = 0; i < data.length; i += 4) if (data[i] > 150 && data[i + 1] < 120) count += 1;
      return count;
    };
    check(red(false) === 0 && red(true) > 0, profile.id, 'the microphones off must show in red, and only then');
  }
}

// Every expression and scene on the previous generation's 320 × 240 screen, for a look.
function showSheet() {
  const sheet = document.getElementById('sheet');
  const layout = fit(320, 240, 'rect');
  const add = (caption, frame) => {
    const figure = document.createElement('figure');
    const canvas = document.createElement('canvas');
    canvas.width = 320;
    canvas.height = 240;
    drawFrame(canvas.getContext('2d'), layout, frame, { status: { battery: 82, charging: false } });
    const label = document.createElement('figcaption');
    label.textContent = caption;
    figure.append(canvas, label);
    sheet.append(figure);
  };
  for (const name of Object.keys(EXPRESSIONS)) add(name, still(name).frame(1.2));
  for (const [name, spec] of Object.entries(SCENES)) {
    const animator = still();
    animator.setScene(name, { ...spec.example, ends_at: Date.now() / 1000 + 200 }, 0);
    add(`scene: ${name}`, animator.frame(2.4));
  }
}

(async () => {
  let summary = '';
  try {
    await checkVocabulary();
    checkExpressions();
    checkScenes();
    checkDrawing();
    checkAnimator();
    checkSceneMotion();
    checkSceneText();
    checkProtocol();
    checkPanel();
    await checkSleep();
    checkPress();
    checkStatusRow();
    showSheet();
    summary = `${Object.keys(EXPRESSIONS).length} expressions · ${Object.keys(SCENES).length} scenes · `
      + `${KINDS.length} panel items · ${PROFILES.length} screens · ${MOMENTS.length} moments · protocol ${PROTOCOL}`;
  } catch (error) {
    fail('tests', error.stack ?? error.message);
  }
  const status = failures.length ? 'fail' : 'pass';
  const result = document.getElementById('result');
  result.dataset.status = status;
  result.textContent = [`${status.toUpperCase()} · ${summary}`, ...failures].join('\n');
  document.title = `${status.toUpperCase()} · ${document.title}`;
})();
