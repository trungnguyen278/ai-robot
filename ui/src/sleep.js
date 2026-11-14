// SPDX-License-Identifier: MIT
/* The robot's sleep, on the page (docs/decisions/0021-sleep-and-wake.md). The software
   decides when the robot sleeps, and says so in the state (`asleep`).

   - Awake, the page holds a Screen Wake Lock, the web's way to keep the screen on, and
     asks for it again whenever the page shows again.
   - Asleep, the face grows sleepy, then closes its eyes, and then the page lets the
     lock go: the kiosk turns the screen off a while later (tools/pi/kiosk/).
   - Woken, the face is the software's again, and the lock is held again. A dark screen
     is lit by the software, or by the touch that woke it. */

export const SLEEPY = 'sleepy'; // the face as the robot falls asleep
export const ASLEEP = 'asleep'; // its face once its eyes have closed

export class Sleep {
  /** `player` shows the face (player.js). `drowsy` and `release` are the seconds of a
      sleepy face before the eyes close, and of closed eyes before the lock goes
      (settings.js). `wakeLock` is the browser's (navigator.wakeLock), or null where it
      has none; `page` is the document, whose visibility the lock follows; `timers`
      sets and clears timeouts. */
  constructor(player, {
    drowsy,
    release,
    wakeLock = globalThis.navigator?.wakeLock ?? null,
    page = globalThis.document ?? null,
    timers = { set: (run, ms) => setTimeout(run, ms), clear: (id) => clearTimeout(id) },
  }) {
    this.player = player;
    this.drowsy = drowsy;
    this.release = release;
    this.wakeLock = wakeLock;
    this.page = page;
    this.timers = timers;
    this.asleep = false;
    this.lock = null; // the lock held, if any
    this.asking = false; // a request for one is under way
    this.pending = []; // the timers of falling asleep
    page?.addEventListener('visibilitychange', () => this.hold());
  }

  /** Follow the software's state: `asleep` as it says. Returns whether the face is
      sleep's, rather than the software's. */
  follow(asleep) {
    if (asleep && !this.asleep) this.fallAsleep();
    else if (!asleep && this.asleep) this.wake();
    else if (!asleep) this.hold();
    return this.asleep;
  }

  fallAsleep() {
    this.asleep = true;
    this.show(SLEEPY);
    this.later(this.drowsy, () => this.show(ASLEEP));
    this.later(this.drowsy + this.release, () => this.letGo());
  }

  wake() {
    this.asleep = false;
    for (const id of this.pending) this.timers.clear(id);
    this.pending = [];
    this.hold();
  }

  show(expression) {
    this.player.animator.set(expression, null, this.player.now());
  }

  later(seconds, run) {
    this.pending.push(this.timers.set(run, seconds * 1000));
  }

  /** Hold the lock while awake and shown; the browser refuses it to a hidden page, and
      lets it go when the page hides. */
  hold() {
    if (this.asleep || this.lock || this.asking || !this.wakeLock) return;
    if (this.page && this.page.visibilityState !== 'visible') return;
    this.asking = true;
    this.wakeLock.request('screen').then((lock) => {
      this.asking = false;
      if (this.asleep) {
        lock.release(); // it fell asleep meanwhile
        return;
      }
      this.lock = lock;
      lock.addEventListener('release', () => {
        this.lock = null;
        // Let go by the browser, it is asked for again: at once if the page shows, or
        // once it shows again. Let go here, the robot sleeps, and hold() asks nothing.
        this.hold();
      });
    }, () => {
      this.asking = false; // refused: hidden, or not allowed; asked again when shown
    });
  }

  letGo() {
    this.lock?.release(); // its release event forgets it
  }
}
