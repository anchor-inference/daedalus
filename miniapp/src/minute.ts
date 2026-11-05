// One clock for every relative time on the page that only changes by the minute: the message times
// in a transcript. A timer per message would be six hundred timers in a long chat, each repainting its
// own row at its own moment; one shared timer repaints them all together, once a minute, and only
// while something on the page reads it. An age can be up to a minute stale, which a label that only
// counts minutes cannot show anyway.

import { useSyncExternalStore } from "react";

let now = Date.now();
let timer: ReturnType<typeof setTimeout> | null = null;
const listeners = new Set<() => void>();

function tick(): void {
  now = Date.now();
  for (const listener of listeners) listener();
}

function start(): void {
  // On the wall clock's minute, so a time shown as a clock or a date turns over with the day itself.
  now = Date.now();
  timer = setTimeout(() => {
    tick();
    timer = setInterval(tick, 60_000);
  }, 60_000 - (now % 60_000));
}

function stop(): void {
  if (timer !== null) {
    clearTimeout(timer);
    clearInterval(timer);
  }
  timer = null;
}

/** Exported for the tests; a component reads the clock through `useMinute`. */
export function subscribeMinute(listener: () => void): () => void {
  listeners.add(listener);
  if (timer === null) start();
  return () => {
    listeners.delete(listener);
    if (!listeners.size) stop();
  };
}

export function minuteNow(): number {
  return now;
}

/** The time of the latest whole minute, which changes once a minute for every reader at once. */
export function useMinute(): number {
  return useSyncExternalStore(subscribeMinute, minuteNow);
}
