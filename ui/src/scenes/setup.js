// SPDX-License-Identifier: MIT
/* Set-up: the code the owner scans or types into the app, to set the robot up over
   Bluetooth LE (platform/protocols/setup/README.md), then how set-up is going.

   Data: `state`, one of STATES:
   - `code`: the code, as a QR code and in digits, with the discriminator that tells
     robots apart. `code` is 8 digits, `discriminator` 4 hexadecimal digits, and `link`
     what the QR code holds;
   - `connected`: an app is setting the robot up;
   - `connecting`: set-up is done, and the robot waits for its server to accept it;
   - `closed`: the robot is not set up, and a tap opens set-up.
   An unknown state reads as `closed`; a code or a link that is not one shows dashes
   or an empty frame. The QR code is drawn dark on light, without glow, so that
   cameras read it. */

import qrcodegen from '../../third_party/qrcodegen/qrcodegen.js';
import { TAU } from '../math.js';
import { BACKGROUND, rgba } from '../tones.js';
import { clip, digits, text } from '../text.js';
import { CORNER, cached, spinner } from './kit.js';

export const STATES = Object.freeze(['code', 'connected', 'connecting', 'closed']);
export const CODE_LENGTH = 8; // digits, as in the protocol's vocabulary
export const DISCRIMINATOR_LENGTH = 4;
export const LINK_LENGTH = 256; // characters, as in the protocol's vocabulary

const CARD = { x: -72, y: 12, side: 136, r: 10 }; // the QR code's light square
const QUIET = 4; // modules of light margin around a QR code (ISO/IEC 18004)
const COLUMN = 80; // the x of the text beside the QR code

const codes = new Map(); // link -> its QR code (null if it cannot be one), most recent last
const CODES = 4;

/** The QR code of `link`, encoded once. */
function qrCode(link) {
  if (codes.has(link)) return codes.get(link);
  let code = null;
  try {
    code = qrcodegen.QrCode.encodeText(link, qrcodegen.QrCode.Ecc.MEDIUM);
  } catch {
    code = null; // too long to be a QR code
  }
  codes.set(link, code);
  if (codes.size > CODES) codes.delete(codes.keys().next().value);
  return code;
}

function drawCard(ctx, link, pixels) {
  const { x, y, side, r } = CARD;
  const box = { x: x - side / 2, y: y - side / 2, w: side, h: side };
  ctx.save();
  ctx.shadowBlur = 0; // a glow would blur the modules
  const code = typeof link === 'string' && link && link.length <= LINK_LENGTH ? qrCode(link) : null;
  cached(ctx, `setup-qr|${code ? link : ''}`, box, pixels, (c) => {
    c.beginPath();
    c.roundRect(box.x, box.y, box.w, box.h, r);
    if (!code) {
      c.lineWidth = 3;
      c.stroke();
      return;
    }
    c.fill();
    const module = side / (code.size + 2 * QUIET);
    c.fillStyle = rgba(BACKGROUND);
    c.beginPath(); // one path, so that touching modules leave no seam
    for (let row = 0; row < code.size; row += 1) {
      for (let col = 0; col < code.size; col += 1) {
        if (code.getModule(col, row)) {
          c.rect(box.x + (QUIET + col) * module, box.y + (QUIET + row) * module, module, module);
        }
      }
    }
    c.fill();
  });
  ctx.restore();
}

function drawCode(ctx, data, alpha, pixels) {
  ctx.globalAlpha = alpha;
  drawCard(ctx, data.link, pixels);
  const code = typeof data.code === 'string' && /^[0-9]{8}$/.test(data.code) ? data.code : '-'.repeat(CODE_LENGTH);
  const half = CODE_LENGTH / 2;
  ctx.globalAlpha = alpha * 0.65;
  text(ctx, 'Set me up in the app', COLUMN, -16, 12, { weight: 500 });
  ctx.globalAlpha = alpha;
  digits(ctx, code.slice(0, half), COLUMN, 20, 32);
  digits(ctx, code.slice(half), COLUMN, 56, 32);
  if (typeof data.discriminator === 'string' && data.discriminator) {
    ctx.globalAlpha = alpha * 0.5;
    text(ctx, clip(data.discriminator, DISCRIMINATOR_LENGTH), COLUMN, 88, 12, { weight: 500 });
  }
}

const WORDS = {
  connected: 'Setting up with the app',
  connecting: 'Connecting',
  closed: 'Tap to set me up',
};

export default {
  tone: 'cyan',
  slot: CORNER,
  example: { state: 'code', code: '12345678', discriminator: '3F7A', link: 'setup:2:3F7A:12345678' },

  draw(ctx, t, { alpha, data, pixels }) {
    const state = STATES.includes(data.state) ? data.state : 'closed';
    if (state === 'code') {
      drawCode(ctx, data, alpha, pixels);
      return;
    }
    if (state === 'closed') {
      ctx.globalAlpha = alpha * (0.75 + 0.25 * Math.cos(TAU * t * 0.4)); // a slow breath
      text(ctx, WORDS.closed, -10, 0, 22, { weight: 600 });
      return;
    }
    spinner(ctx, -10, -10, 34, t, alpha);
    ctx.globalAlpha = alpha * 0.75;
    text(ctx, WORDS[state], -10, 52, 18, { weight: 600 });
  },
};
