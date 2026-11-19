// SPDX-License-Identifier: MIT
/* Reset: the robot's factory reset, from the moment its owner chose Erase in its
   settings until it starts again as a new robot (docs/decisions/0023-factory-reset.md).

   Data: `state`, one of STATES:
   - `leaving`: the robot leaves its home, so that the app no longer lists it;
   - `not_left`: it could not, being offline, so the owner must remove it in the app;
     it erases itself anyway a few seconds later;
   - `erasing`: it erases what it keeps.
   An unknown state reads as `erasing`, which follows whatever came before. */

import { text } from '../text.js';
import { CORNER, spinner } from './kit.js';

export const STATES = Object.freeze(['leaving', 'not_left', 'erasing']);

const WORDS = {
  leaving: 'Leaving your home',
  erasing: 'Erasing',
};
const NOT_LEFT = ['Remove me in the app', 'I could not leave your home'];

const state = (data) => (STATES.includes(data.state) ? data.state : 'erasing');

export default {
  tone: (data) => (state(data) === 'not_left' ? 'orange' : 'cyan'),
  slot: CORNER,
  example: { state: 'not_left' },

  draw(ctx, t, { alpha, data }) {
    const now = state(data);
    if (now === 'not_left') {
      const [act, why] = NOT_LEFT;
      ctx.globalAlpha = alpha;
      text(ctx, act, -10, -8, 22, { weight: 600 });
      ctx.globalAlpha = alpha * 0.65;
      text(ctx, why, -10, 24, 13, { weight: 500 });
      return;
    }
    spinner(ctx, -10, -10, 34, t, alpha);
    ctx.globalAlpha = alpha * 0.75;
    text(ctx, WORDS[now], -10, 52, 18, { weight: 600 });
  },
};
