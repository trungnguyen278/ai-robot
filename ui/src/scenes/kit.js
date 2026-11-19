// SPDX-License-Identifier: MIT
/* Shared tools for the scenes: where the face waits, time, the battery, shapes, a
   spinner, and a cache for drawing that never changes. Text is in ../text.js. */

import { TAU, clamp } from '../math.js';
import { BACKGROUND, rgba } from '../tones.js';

// Where the face waits while a scene fills the screen: small, in the top right
// corner, watching (the previous generation's scene design). Face units, from the
// centre of the face area; inside the disc of a round screen too.
export const CORNER = Object.freeze({ x: 110, y: -78, scale: 0.3 });

const pad = (n) => String(n).padStart(2, '0');

/** A ring with a turning arc, centred on x, y: something is going on. */
export function spinner(ctx, x, y, r, t, alpha) {
  ctx.lineWidth = 6;
  ctx.lineCap = 'round';
  ctx.globalAlpha = alpha * 0.2;
  ctx.beginPath();
  ctx.arc(x, y, r, 0, TAU);
  ctx.stroke();
  ctx.globalAlpha = alpha;
  ctx.beginPath();
  const start = TAU * ((t * 0.8) % 1);
  ctx.arc(x, y, r, start, start + TAU * (0.25 + 0.1 * Math.sin(TAU * t * 0.5)));
  ctx.stroke();
}

/** "4:05", or "1:04:05" from an hour up. */
export function duration(seconds) {
  const s = Math.max(0, Math.ceil(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h ? `${h}:${pad(m)}:${pad(s % 60)}` : `${m}:${pad(s % 60)}`;
}

export function hhmm(date) {
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** The battery's charge as a whole percentage, or null if unknown. */
export function batteryPercent({ battery } = {}) {
  return Number.isFinite(battery) ? Math.round(clamp(battery, 0, 100)) : null;
}

/** The path of a lightning bolt `size` tall, centred on cx, cy. */
export function boltPath(ctx, cx, cy, size) {
  ctx.beginPath();
  ctx.moveTo(cx + 0.15 * size, cy - 0.5 * size);
  ctx.lineTo(cx - 0.3 * size, cy + 0.08 * size);
  ctx.lineTo(cx, cy + 0.08 * size);
  ctx.lineTo(cx - 0.15 * size, cy + 0.5 * size);
  ctx.lineTo(cx + 0.3 * size, cy - 0.08 * size);
  ctx.lineTo(cx, cy - 0.08 * size);
  ctx.closePath();
}

/** Fill the current path, cut out from what is behind it by a gap of `gap` units. */
export function cutOut(ctx, gap) {
  ctx.save();
  ctx.shadowBlur = 0;
  ctx.strokeStyle = rgba(BACKGROUND);
  ctx.lineWidth = 2 * gap;
  ctx.lineJoin = 'round';
  ctx.stroke();
  ctx.restore(); // the path is not part of the saved state: it is still there
  ctx.fill();
}

const bitmaps = new Map(); // most recently used last
const BITMAPS = 16;

/** Draw something that never changes, glow included, from a bitmap made once.

    The glow, a blur on every shape, is by far the most expensive part of a frame;
    a static drawing pays it once per size and colour instead of on every frame.
    `paint(ctx)` draws inside `box` ({ x, y, w, h }, glow excluded) in the units of
    `ctx`'s present transform, with its colours and glow; `pixels` is the device
    pixels per unit at full size (the bitmap's resolution). */
export function cached(ctx, key, box, pixels, paint) {
  const margin = Math.ceil(1.5 * ctx.shadowBlur) + 2; // device pixels for the glow
  const id = [key, pixels.toFixed(3), ctx.fillStyle, ctx.strokeStyle, ctx.shadowColor, ctx.shadowBlur].join('|');
  let bitmap = bitmaps.get(id);
  if (bitmap) bitmaps.delete(id);
  else {
    const width = Math.ceil(box.w * pixels) + 2 * margin;
    const height = Math.ceil(box.h * pixels) + 2 * margin;
    bitmap = typeof OffscreenCanvas === 'function'
      ? new OffscreenCanvas(width, height)
      : Object.assign(document.createElement('canvas'), { width, height });
    const c = bitmap.getContext('2d');
    for (const style of ['fillStyle', 'strokeStyle', 'shadowColor', 'shadowBlur']) c[style] = ctx[style];
    c.setTransform(pixels, 0, 0, pixels, margin - box.x * pixels, margin - box.y * pixels);
    paint(c);
  }
  bitmaps.set(id, bitmap);
  if (bitmaps.size > BITMAPS) bitmaps.delete(bitmaps.keys().next().value);
  ctx.save();
  ctx.shadowBlur = 0; // the glow is in the bitmap
  const edge = margin / pixels;
  ctx.drawImage(bitmap, box.x - edge, box.y - edge, bitmap.width / pixels, bitmap.height / pixels);
  ctx.restore();
}
