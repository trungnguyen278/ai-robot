// SPDX-License-Identifier: MIT
/* Panels: screens the owner acts on, drawn over the face, such as the settings
   (protocol/screen/README.md, Panels). The software describes a panel; this draws it
   and reports what the owner does, with `onAction(panel, item, value)` and
   `onClose(panel)`. A change shows at once, and the software's next state shows the
   panel as the robot took it: the view keeps nothing the software did not send.

   In HTML rather than on the canvas, for what the browser brings: scrolling, focus,
   controls a finger can work, and reading aloud. */

import { SETTINGS } from './settings.js';
import { font } from './text.js';
import { BACKGROUND, TONES, rgba } from './tones.js';

export const KINDS = Object.freeze(['toggle', 'slider', 'choice', 'button', 'info']);

// The smallest touch target, in CSS pixels: Material Design asks for 48 × 48 dp.
export const TARGET = 48;
// On a round screen the content keeps to the square inside the circle, which leaves
// this fraction of the diameter on each side.
const ROUND_INSET = (1 - Math.SQRT1_2) / 2;
const ACCENT = TONES.cyan.rgb; // the calm default tone
const INK = TONES.white.rgb;
const STYLE = 'panel-style'; // the id of the style sheet, added once to a page

let views = 0; // on the page, for unique element ids

// The settings much as the robot shows them (software/src/ai_robot/modules/), for the
// studio and the tests.
export const EXAMPLE = Object.freeze({
  name: 'settings',
  title: 'Settings',
  sections: [
    { title: 'Wi-Fi and app', items: [
      { id: 'setup.start', kind: 'button', label: 'Set up with the app',
        help: 'For a new Wi-Fi network or a new phone: shows a code to scan' },
      { id: 'setup.network', kind: 'info', label: 'Wi-Fi', value: 'Home, connected' },
    ] },
    { title: 'Sound', items: [
      { id: 'voice.volume', kind: 'slider', label: 'Volume', value: 80, min: 0, max: 100, step: 5, unit: '%' },
      { id: 'voice.microphones', kind: 'toggle', label: 'Microphones', value: true,
        help: 'Off, the robot hears nothing until they are on again' },
    ] },
    { title: 'What the cloud may do', items: [
      { id: 'cloud.informational', kind: 'toggle', label: 'Show things and play sounds', value: true },
      { id: 'cloud.privacy', kind: 'toggle', label: 'Use the camera, start conversations', value: false,
        confirm: 'Let the cloud use the camera and start conversations?' },
      { id: 'cloud.physical', kind: 'toggle', label: 'Move the robot', value: false,
        confirm: 'Let the cloud move the robot?' },
      { id: 'cloud.system', kind: 'toggle', label: 'Restart, update, change settings', value: false,
        confirm: 'Let the cloud restart and update the robot?' },
    ] },
    { title: 'About', items: [
      { id: 'settings.name', kind: 'info', label: 'Name', value: 'Robot' },
      { id: 'cloud.account', kind: 'info', label: "Owner's account", value: 'Set up' },
    ] },
  ],
});

function styleSheet() {
  const accent = (alpha) => rgba(ACCENT, alpha);
  const ink = (alpha) => rgba(INK, alpha);
  const round = `${(ROUND_INSET * 100).toFixed(2)}vmin`;
  return `
.panel { position: fixed; inset: 0; z-index: 10; display: flex; flex-direction: column;
  background: ${rgba(BACKGROUND, 0.92)}; color: ${ink(1)}; font: ${font(18, 500)}; line-height: 1.35;
  touch-action: pan-y; user-select: none; -webkit-user-select: none; }
.panel.round { padding: ${round}; }
.panel[hidden] { display: none; }
.panel header { display: flex; align-items: center; justify-content: space-between; gap: 16px;
  padding: 8px 8px 8px 24px; border-bottom: 1px solid ${accent(0.2)}; }
.panel h1 { margin: 0; font-size: 24px; font-weight: 700; color: ${accent(1)}; }
.panel .body { flex: 1; overflow-y: auto; overscroll-behavior: contain; padding: 0 24px 32px; }
.panel h2 { margin: 20px 0 0; font-size: 14px; font-weight: 700; letter-spacing: 0.06em;
  text-transform: uppercase; color: ${accent(0.75)}; }
.panel button { font: inherit; color: inherit; background: none; border: 0; padding: 0;
  min-width: ${TARGET}px; min-height: ${TARGET}px; cursor: pointer; }
.panel :focus-visible { outline: 2px solid ${accent(1)}; outline-offset: 2px; }
.panel .done { padding: 0 16px; font-weight: 700; color: ${accent(1)}; }
.panel .item { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between;
  column-gap: 16px; width: 100%; min-height: ${TARGET + 8}px; padding: 6px 0; text-align: left;
  border-bottom: 1px solid ${ink(0.08)}; }
.panel .text { flex: 1; min-width: 0; }
.panel .help { font-size: 14px; color: ${ink(0.6)}; }
.panel .value { color: ${ink(0.75)}; text-align: right; overflow-wrap: anywhere; }
.panel .switch { position: relative; flex: none; width: 72px; }
.panel .switch::before { content: ""; position: absolute; inset: 12px 8px; border-radius: 12px;
  background: ${ink(0.22)}; transition: background 0.15s; }
.panel .switch::after { content: ""; position: absolute; top: 16px; left: 12px; width: 16px; height: 16px;
  border-radius: 50%; background: ${ink(1)}; transition: left 0.15s; }
.panel .switch[aria-checked="true"]::before { background: ${accent(0.85)}; }
.panel .switch[aria-checked="true"]::after { left: 44px; }
.panel .slider input { flex-basis: 100%; height: ${TARGET}px; margin: 0; accent-color: ${accent(1)}; }
.panel .choices { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
.panel .choices button { padding: 0 18px; border: 1px solid ${accent(0.4)}; border-radius: ${TARGET / 2}px; }
.panel .choices button[aria-checked="true"] { background: ${accent(0.2)}; color: ${accent(1)}; }
.panel .button .label { color: ${accent(1)}; font-weight: 600; }
.panel .question { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  padding: 24px; background: ${rgba(BACKGROUND, 0.7)}; }
.panel.round .question { padding: ${round}; }
.panel .question[hidden] { display: none; }
.panel .box { max-width: min(480px, 100%); padding: 24px; border-radius: 16px;
  background: ${rgba(BACKGROUND, 1)}; border: 1px solid ${accent(0.4)}; }
.panel .box p { margin: 0 0 16px; font-size: 20px; }
.panel .box .lines { margin: 0 0 16px; }
.panel .box .lines p { margin: 0 0 8px; font-size: 16px; color: ${ink(0.75)}; }
.panel .answers { display: flex; justify-content: flex-end; gap: 12px; }
.panel .answers button { padding: 0 20px; border-radius: ${TARGET / 2}px; }
.panel .answers .yes { background: ${accent(0.2)}; color: ${accent(1)}; font-weight: 700; }
`;
}

const text = (value) => (typeof value === 'string' ? value : '');
const isObject = (value) => value !== null && typeof value === 'object';
const sameValue = (a, b) => JSON.stringify(a) === JSON.stringify(b);

/** The sections of `panel` with the items this renderer draws, each id once. */
function sectionsOf(panel) {
  const seen = new Set();
  return panel.sections.filter(isObject).map((section) => ({
    title: text(section.title),
    items: (Array.isArray(section.items) ? section.items : []).filter((item) => {
      const drawn = isObject(item) && typeof item.id === 'string' && KINDS.includes(item.kind) && !seen.has(item.id);
      if (drawn) seen.add(item.id);
      return drawn;
    }),
  })).filter((section) => section.items.length > 0);
}

// All a panel shows but its values: a panel sent again with the same layout is
// updated in place, where the owner left it; one with another is drawn anew.
function layoutOf(panel, sections) {
  return JSON.stringify([panel.name, text(panel.title), sections.map(({ title, items }) => [title,
    items.map((item) => [item.id, item.kind, item.label, item.help, item.min, item.max, item.step,
      item.unit, item.choices])])]);
}

export class PanelView {
  /** Draws panels in `root`, an element it fills while one shows. `shape` is the
      screen's, `rect` or `round`. A panel left untouched for `idle` seconds closes;
      `timers` stands in for setTimeout and clearTimeout, so that tests need not wait. */
  constructor(root, {
    shape = 'rect',
    idle = SETTINGS.panel.idle,
    onAction = () => {},
    onClose = () => {},
    timers = { set: (run, ms) => setTimeout(run, ms), clear: (id) => clearTimeout(id) },
  } = {}) {
    this.root = root;
    this.document = root.ownerDocument;
    this.idle = idle;
    this.onAction = onAction;
    this.onClose = onClose;
    this.timers = timers;
    this.id = `panel-${++views}`;
    this.panel = null; // on screen, as the software sent it
    this.layout = null;
    this.updates = new Map(); // item id -> how to show a new value of it
    this.timer = null;
    this.body = null;
    this.question = null;
    this.dismiss = null; // takes a question away, while one is asked
    if (!this.document.getElementById(STYLE)) {
      const sheet = this.document.createElement('style');
      sheet.id = STYLE;
      sheet.textContent = styleSheet();
      this.document.head.append(sheet);
    }
    root.classList.add('panel');
    this.setShape(shape);
    root.setAttribute('role', 'dialog');
    root.setAttribute('aria-modal', 'true');
    root.setAttribute('aria-labelledby', `${this.id}-title`);
    root.hidden = true;
    // Only the owner's touches keep a panel open; the software's updates do not.
    for (const type of ['pointerdown', 'keydown', 'wheel']) {
      root.addEventListener(type, () => this.awake(), { capture: true, passive: true });
    }
    root.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape') return;
      if (this.dismiss) this.dismiss();
      else this.close();
    });
  }

  /** The screen's shape, `rect` or `round`. */
  setShape(shape) {
    this.root.classList.toggle('round', shape === 'round');
  }

  /** Show `panel` as the software sent it, or none (`null`). */
  show(panel) {
    if (!isObject(panel) || typeof panel.name !== 'string' || !panel.name || !Array.isArray(panel.sections)) {
      this.hide();
      return;
    }
    const sections = sectionsOf(panel);
    const layout = layoutOf(panel, sections);
    const opening = this.panel?.name !== panel.name;
    if (layout === this.layout) {
      for (const { items } of sections) for (const item of items) this.updates.get(item.id)?.(item);
    } else {
      const scroll = opening ? 0 : this.body.scrollTop;
      this.draw(panel, sections);
      this.layout = layout;
      this.root.hidden = false;
      this.body.scrollTop = scroll;
    }
    this.panel = panel;
    this.root.hidden = false;
    if (opening) this.awake();
  }

  /** Take the panel away, without a word to the software. */
  hide() {
    if (this.timer !== null) this.timers.clear(this.timer);
    this.timer = null;
    this.panel = null;
    this.layout = null;
    this.updates.clear();
    this.body = null;
    this.question = null;
    this.dismiss = null;
    this.root.hidden = true;
    this.root.replaceChildren();
  }

  /** The owner closes the panel: it goes at once, and the software is told. */
  close() {
    const name = this.panel?.name;
    this.hide();
    if (name) this.onClose(name);
  }

  /** The owner touched the panel: it stays a while longer. */
  awake() {
    if (this.timer !== null) this.timers.clear(this.timer);
    this.timer = this.panel ? this.timers.set(() => this.close(), this.idle * 1000) : null;
  }

  act(id, value) {
    if (this.panel) this.onAction(this.panel.name, id, value);
  }

  // ---- drawing ----

  element(tag, className = '', content = null) {
    const element = this.document.createElement(tag);
    if (className) element.className = className;
    if (content !== null) element.textContent = content;
    return element;
  }

  control(className, content, onClick) {
    const button = this.element('button', className, content);
    button.type = 'button';
    button.addEventListener('click', onClick);
    return button;
  }

  draw(panel, sections) {
    this.updates.clear();
    this.dismiss = null;
    const title = this.element('h1', '', text(panel.title));
    title.id = `${this.id}-title`;
    const header = this.element('header');
    header.append(title, this.control('done', 'Done', () => this.close()));
    this.body = this.element('div', 'body');
    for (const section of sections) {
      const group = this.element('section');
      group.append(this.element('h2', '', section.title), ...section.items.map((item) => this.item(item)));
      this.body.append(group);
    }
    this.question = this.element('div', 'question');
    this.question.hidden = true;
    this.question.addEventListener('click', (event) => {
      if (event.target === this.question) this.dismiss?.(); // a tap beside the question
    });
    this.root.replaceChildren(header, this.body, this.question);
  }

  item(item) {
    switch (item.kind) {
      case 'toggle': return this.toggle(item);
      case 'slider': return this.slider(item);
      case 'choice': return this.choice(item);
      case 'button': return this.button(item);
      default: return this.info(item);
    }
  }

  /** The row of `item`: its label, with its help under it. A button's row is a
      `button` element, the others a `div`. */
  row(item, tag = 'div') {
    const row = this.element(tag, `item ${item.kind}`);
    const words = this.element('div', 'text');
    words.append(this.element('div', 'label', text(item.label)));
    if (text(item.help)) words.append(this.element('div', 'help', item.help));
    row.append(words);
    return row;
  }

  toggle(item) {
    let current = item;
    const row = this.row(item);
    const control = this.element('button', 'switch');
    control.type = 'button';
    control.setAttribute('role', 'switch');
    control.setAttribute('aria-label', text(item.label));
    const set = (on) => control.setAttribute('aria-checked', String(on));
    set(item.value === true);
    control.addEventListener('click', () => {
      const on = control.getAttribute('aria-checked') !== 'true';
      const go = () => {
        set(on);
        this.act(item.id, on);
      };
      if (on && text(current.confirm)) this.ask(current, go);
      else go();
    });
    this.updates.set(item.id, (next) => {
      current = next;
      set(next.value === true);
    });
    row.append(control);
    return row;
  }

  slider(item) {
    const row = this.row(item);
    const output = this.element('output', 'value');
    const input = this.element('input');
    input.type = 'range';
    input.setAttribute('aria-label', text(item.label));
    const low = Number.isFinite(item.min) ? item.min : 0;
    const high = Number.isFinite(item.max) && item.max > low ? item.max : low + 1;
    input.min = String(low);
    input.max = String(high);
    input.step = Number.isFinite(item.step) && item.step > 0 ? String(item.step) : 'any';
    const unit = text(item.unit);
    const shown = () => {
      output.textContent = `${input.value}${unit}`;
    };
    const set = (value) => {
      input.value = String(Number.isFinite(value) ? value : low);
      shown();
    };
    let known = item.value; // the software's latest value
    let pressed = null; // the value as the finger came down, while it is on the slider
    set(known);
    // While the finger is on the slider, the slider stays where the finger is.
    input.addEventListener('pointerdown', () => {
      pressed = input.value;
    });
    const lift = (cancelled) => {
      if (pressed === null) return;
      const moved = input.value !== pressed;
      pressed = null;
      if (cancelled || !moved) set(known); // no change follows: back to the software's value
    };
    input.addEventListener('pointerup', () => lift(false));
    input.addEventListener('pointercancel', () => lift(true));
    input.addEventListener('input', shown);
    input.addEventListener('change', () => this.act(item.id, Number(input.value)));
    this.updates.set(item.id, (next) => {
      known = next.value;
      if (pressed === null) set(known);
    });
    row.append(output, input);
    return row;
  }

  choice(item) {
    const row = this.row(item);
    const group = this.element('div', 'choices');
    group.setAttribute('role', 'radiogroup');
    group.setAttribute('aria-label', text(item.label));
    const options = (Array.isArray(item.choices) ? item.choices : [])
      .filter((option) => isObject(option) && 'value' in option);
    let chosen = item.value;
    const buttons = options.map((option) => {
      const button = this.control('', text(option.label) || String(option.value), () => {
        if (sameValue(option.value, chosen)) return;
        set(option.value);
        this.act(item.id, option.value);
      });
      button.setAttribute('role', 'radio');
      return button;
    });
    const set = (value) => {
      chosen = value;
      buttons.forEach((button, i) => button.setAttribute('aria-checked', String(sameValue(options[i].value, value))));
    };
    set(item.value);
    this.updates.set(item.id, (next) => set(next.value));
    group.append(...buttons);
    row.append(group);
    return row;
  }

  button(item) {
    let current = item;
    const row = this.row(item, 'button');
    row.type = 'button';
    row.addEventListener('click', () => {
      const go = () => this.act(item.id, undefined);
      if (text(current.confirm)) this.ask(current, go);
      else go();
    });
    this.updates.set(item.id, (next) => {
      current = next;
    });
    return row;
  }

  info(item) {
    const row = this.row(item);
    const value = this.element('div', 'value', text(item.value));
    this.updates.set(item.id, (next) => {
      value.textContent = text(next.value);
    });
    row.append(value);
    return row;
  }

  /** Ask the question of `item` over the panel, with the lines under it and the label
      of the answer that goes ahead; `then` runs if the owner goes ahead. */
  ask(item, then) {
    const end = () => {
      this.dismiss = null;
      this.question.hidden = true;
      this.question.replaceChildren();
    };
    const words = this.element('p', '', item.confirm);
    words.id = `${this.id}-question`;
    const lines = this.element('div', 'lines');
    lines.id = `${this.id}-lines`;
    for (const line of Array.isArray(item.confirm_lines) ? item.confirm_lines : []) {
      if (text(line)) lines.append(this.element('p', '', line));
    }
    const no = this.control('no', 'Cancel', end);
    const yes = this.control('yes', text(item.confirm_label) || 'Yes', () => {
      end();
      then();
    });
    const answers = this.element('div', 'answers');
    answers.append(no, yes);
    const box = this.element('div', 'box');
    box.setAttribute('role', 'alertdialog');
    box.setAttribute('aria-modal', 'true');
    box.setAttribute('aria-labelledby', words.id);
    box.append(words);
    if (lines.childElementCount) {
      box.setAttribute('aria-describedby', lines.id);
      box.append(lines);
    }
    box.append(answers);
    this.question.replaceChildren(box);
    this.question.hidden = false;
    this.dismiss = end;
    no.focus(); // the safe answer, for a key pressed without looking
  }
}
