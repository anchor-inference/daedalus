// One download per file, however many places ask for it at once. A file card's image thumbnail and
// the panel's viewer read the same bytes; a card pressed twice, or a viewer React mounts, unmounts
// and mounts again in the same moment, asked the host for the same file twice. Every asker joins
// the request already under way and hears its progress; the request is cancelled only when the
// last of them has gone. Nothing is kept once it has finished: a file the agent rewrites a second
// later must come back fresh, and the viewer's Reload must mean a new read.

export type Progress = (value: number | null) => void;

type Flight<T> = { promise: Promise<T>; controller: AbortController; listeners: Set<Progress>; users: number; last: number | null };

export type Joined<T> = { promise: Promise<T>; release: () => void };

export function createSharedFetch<T>() {
  const flights = new Map<string, Flight<T>>();

  /** Join the request for `key`, starting it with `start` when none is under way. `release` says
   *  this asker no longer wants the answer; the request is aborted when nobody does. */
  function join(key: string, start: (signal: AbortSignal, progress: Progress) => Promise<T>, onProgress?: Progress): Joined<T> {
    let flight = flights.get(key);
    if (!flight) {
      const controller = new AbortController();
      const listeners = new Set<Progress>();
      const fresh: Flight<T> = { promise: Promise.resolve() as Promise<T>, controller, listeners, users: 0, last: null };
      fresh.promise = start(controller.signal, (value) => {
        fresh.last = value;
        for (const listener of listeners) listener(value);
      }).finally(() => {
        if (flights.get(key) === fresh) flights.delete(key);
      });
      // A request every asker left rejects with an abort nobody listens for; say so here, or the
      // browser reports it as unhandled.
      fresh.promise.catch(() => undefined);
      flights.set(key, fresh);
      flight = fresh;
    } else if (onProgress && flight.last !== null) {
      onProgress(flight.last);
    }
    const joined = flight;
    joined.users += 1;
    if (onProgress) joined.listeners.add(onProgress);
    let released = false;
    return {
      promise: joined.promise,
      release: () => {
        if (released) return;
        released = true;
        joined.users -= 1;
        if (onProgress) joined.listeners.delete(onProgress);
        if (joined.users > 0) return;
        // Forget it before aborting: an asker that arrives after this must start a new request,
        // not join one that is about to reject.
        if (flights.get(key) === joined) flights.delete(key);
        joined.controller.abort();
      },
    };
  }

  return { join, inFlight: () => flights.size };
}
