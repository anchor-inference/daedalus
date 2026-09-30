// A small cache in front of the API: a screen renders what it showed last time the moment it
// opens, refreshes in the background, and polls while it stays open. Nothing here survives a
// reload; the point is that switching screens never flashes an empty frame.

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api } from "./api";
import { asSentence } from "./format";

type Entry = { data: unknown; at: number; error: string | null };

const cache = new Map<string, Entry>();
const listeners = new Map<string, Set<() => void>>();
const inflight = new Map<string, Promise<unknown>>();
/** How many reads of a key in a row have failed with nothing yet to show, for the retry below. */
const misses = new Map<string, number>();

/** When a read that failed with nothing on screen is tried again: a second, then twice as long each
 *  time up to eight seconds, and never later than the key's own poll would have tried it.
 *
 *  A first read that failed used to wait for the poll: a member's page read its member every 15 s,
 *  so one dropped request left it on "Loading" for fifteen seconds — which is exactly how long a
 *  browser check gave it before calling the page broken. */
export function retryDelay(miss: number, pollMs: number): number {
  const delay = Math.min(1000 * 2 ** Math.max(0, miss - 1), 8000);
  return pollMs > 0 ? Math.min(delay, pollMs) : delay;
}

/** Whether a failure is worth retrying by itself: the host was not reached or failed, not a refusal
 *  (a 404 for a service this installation does not have stays an answer until the next poll). */
function transient(e: unknown): boolean {
  return !(e instanceof ApiError) || e.status >= 500;
}

function notify(key: string) {
  for (const l of listeners.get(key) ?? []) l();
}

export function peek<T>(key: string): T | undefined {
  return cache.get(key)?.data as T | undefined;
}

/** Puts a value into the cache without a request (an optimistic update, a fresher copy from a mutation). */
export function prime<T>(key: string, data: T): void {
  cache.set(key, { data, at: Date.now(), error: null });
  notify(key);
}

/** Marks every entry under the prefix stale (the data stays so nothing flashes) and asks the mounted readers to refresh. */
export function invalidate(prefix: string): void {
  for (const [key, entry] of cache) if (key.startsWith(prefix)) cache.set(key, { ...entry, at: 0 });
  for (const key of listeners.keys()) if (key.startsWith(prefix)) notify(`refresh:${key}`);
}

/** Keys with a local change in flight: a response that lands meanwhile must not overwrite the optimistic copy. */
const held = new Map<string, number>();
export function hold(key: string): void {
  held.set(key, (held.get(key) ?? 0) + 1);
}
export function release(key: string): void {
  const n = (held.get(key) ?? 1) - 1;
  if (n <= 0) held.delete(key);
  else held.set(key, n);
}

async function fetchInto<T>(key: string): Promise<T> {
  const running = inflight.get(key);
  if (running) return running as Promise<T>;
  const p = api
    .get<T>(key)
    .then((data) => {
      // Only the newest request for a key writes; a slower, older one has nothing to add.
      setOffline(false);
      misses.delete(key);
      if (inflight.get(key) === p && !held.has(key)) {
        cache.set(key, { data, at: Date.now(), error: null });
        notify(key);
      }
      return data;
    })
    .catch((e: Error) => {
      if (!(e instanceof ApiError)) setOffline(true);
      else setOffline(false);
      const prev = cache.get(key);
      if (prev?.data === undefined && transient(e)) misses.set(key, (misses.get(key) ?? 0) + 1);
      if (inflight.get(key) === p) {
        cache.set(key, { data: prev?.data, at: prev?.at ?? 0, error: asSentence(e.message || "Request failed") });
        notify(key);
      }
      throw e;
    })
    .finally(() => {
      if (inflight.get(key) === p) inflight.delete(key);
    });
  inflight.set(key, p);
  return p;
}

/** Whether the last request reached the bot at all: a network failure flips it, any answer flips it back. */
let offline = false;
const offlineListeners = new Set<() => void>();
function setOffline(v: boolean) {
  if (offline === v) return;
  offline = v;
  for (const l of offlineListeners) l();
}
export function useOffline(): boolean {
  const [, force] = useState(0);
  useEffect(() => {
    const on = () => force((n) => n + 1);
    offlineListeners.add(on);
    return () => {
      offlineListeners.delete(on);
    };
  }, []);
  return offline;
}

export type Query<T> = { data: T | undefined; error: string | null; loading: boolean; refresh: () => Promise<void> };

/**
 * `key` is the API path. `pollMs` refreshes while the component is mounted and the tab is visible;
 * `staleMs` decides whether the cached copy is fresh enough to skip the first request.
 */
export function useQuery<T>(key: string | null, opts: { pollMs?: number; staleMs?: number } = {}): Query<T> {
  const { pollMs = 0, staleMs = 0 } = opts;
  const [, force] = useState(0);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const refresh = useCallback(async () => {
    if (!key) return;
    try {
      await fetchInto<T>(key);
    } catch {
      /* the entry carries the error */
    }
  }, [key]);
  useEffect(() => {
    if (!key) return;
    const on = () => mounted.current && force((n) => n + 1);
    const set = listeners.get(key) ?? new Set();
    set.add(on);
    listeners.set(key, set);
    const refreshers = listeners.get(`refresh:${key}`) ?? new Set();
    const onRefresh = () => void refresh();
    refreshers.add(onRefresh);
    listeners.set(`refresh:${key}`, refreshers);
    const entry = cache.get(key);
    if (!entry || Date.now() - entry.at > staleMs) void refresh();
    let timer: number | undefined;
    if (pollMs > 0) {
      timer = window.setInterval(() => {
        if (document.visibilityState === "visible") void refresh();
      }, pollMs);
    }
    const onVisible = () => {
      if (document.visibilityState === "visible" && pollMs > 0) void refresh();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      set.delete(on);
      refreshers.delete(onRefresh);
      if (!set.size) listeners.delete(key);
      if (!refreshers.size) listeners.delete(`refresh:${key}`);
      if (timer) window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [key, pollMs, staleMs, refresh]);
  // Nothing to show yet and the last read failed on the way: try again soon rather than at the poll.
  // `miss` changes with every failure, so each one schedules the next attempt.
  const miss = key ? misses.get(key) ?? 0 : 0;
  useEffect(() => {
    if (!key || miss === 0) return;
    const timer = window.setTimeout(() => void refresh(), retryDelay(miss, pollMs));
    return () => window.clearTimeout(timer);
  }, [key, miss, pollMs, refresh]);
  const entry = key ? cache.get(key) : undefined;
  return { data: entry?.data as T | undefined, error: entry?.error ?? null, loading: !!key && !entry, refresh };
}
