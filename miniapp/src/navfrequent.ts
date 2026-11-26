// What the More launcher puts under "Frequent": the destinations this browser has seen opened most
// over the last month, then, while there is not enough history yet, the places the sidebar does not
// already show, so a fresh installation is not a row of empty slots. What the operator wants held in
// place is pinned to the sidebar, which is the host's configuration and not this file's business.
//
// Kept in this browser on purpose: it is a habit of one screen (a phone opens other things than the
// desktop does), and it is not worth a round trip to the host and a field in the configuration.
// The pure functions below take the state and the clock as arguments so a test can drive them; the
// storage and the hook at the foot are the only parts that touch the browser.

import { useEffect, useRef, useSyncExternalStore } from "react";

/** How far back a visit still counts, in days. */
export const WINDOW_DAYS = 30;
/** How many ids the history keeps, so a long-lived browser does not grow it without bound. */
export const MAX_IDS = 200;

/** Visits per id per day (days since the epoch). */
export type FrequentState = { visits: Record<string, Record<string, number>> };

export const EMPTY: FrequentState = { visits: {} };

const DAY = 86_400_000;

export function dayOf(now: number): number {
  return Math.floor(now / DAY);
}

/** A visit's weight falls from 1 today to nothing at the window's edge, so last week's habit
 *  outweighs one burst three weeks ago without the burst vanishing overnight. */
export function score(days: Record<string, number> | undefined, now: number): number {
  if (!days) return 0;
  const today = dayOf(now);
  let total = 0;
  for (const [day, count] of Object.entries(days)) {
    // A day ahead of today is a clock that was moved back: counted as today rather than lost.
    const age = Math.max(0, today - Number(day));
    if (age >= WINDOW_DAYS || !(count > 0)) continue;
    total += count * (1 - age / WINDOW_DAYS);
  }
  return total;
}

function lastDay(days: Record<string, number> | undefined): number {
  return days ? Math.max(-1, ...Object.keys(days).map(Number)) : -1;
}

/** The history with every day outside the window dropped, and an id left with none dropped too. */
export function prune(state: FrequentState, now: number): FrequentState {
  const today = dayOf(now);
  const visits: FrequentState["visits"] = {};
  for (const [id, days] of Object.entries(state.visits)) {
    const kept = Object.fromEntries(Object.entries(days).filter(([day, count]) => today - Number(day) < WINDOW_DAYS && count > 0));
    if (Object.keys(kept).length) visits[id] = kept;
  }
  const ids = Object.keys(visits);
  if (ids.length > MAX_IDS) {
    // The least used go first.
    ids.sort((a, b) => score(visits[a], now) - score(visits[b], now) || lastDay(visits[a]) - lastDay(visits[b]));
    for (const id of ids.slice(0, ids.length - MAX_IDS)) delete visits[id];
  }
  return { visits };
}

export function recordVisit(state: FrequentState, id: string, now: number): FrequentState {
  const day = String(dayOf(now));
  const days = state.visits[id] ?? {};
  return prune({ visits: { ...state.visits, [id]: { ...days, [day]: (days[day] ?? 0) + 1 } } }, now);
}

/** The ids for the chips, at most `limit`, from `candidates` only (what is shown: hidden items are
 *  never offered). The learned ones by score first (the more recent visit breaks a tie, then the
 *  candidates' own order), then `fallback` in its order. */
export function frequent(state: FrequentState, candidates: string[], now: number, limit: number, fallback: string[] = []): string[] {
  const allowed = new Set(candidates);
  const out: string[] = [];
  const add = (id: string) => {
    if (out.length < limit && allowed.has(id) && !out.includes(id)) out.push(id);
  };
  const order = new Map(candidates.map((id, i) => [id, i]));
  candidates
    .map((id) => ({ id, score: score(state.visits[id], now), last: lastDay(state.visits[id]) }))
    .filter((it) => it.score > 0)
    .sort((a, b) => b.score - a.score || b.last - a.last || order.get(a.id)! - order.get(b.id)!)
    .forEach((it) => add(it.id));
  fallback.forEach(add);
  return out;
}

/** Reads what was stored, keeping only what has the right shape: a hand-edited or older value
 *  must not take the launcher down. */
export function parse(text: string | null): FrequentState {
  if (!text) return EMPTY;
  try {
    const raw = JSON.parse(text) as unknown;
    if (!raw || typeof raw !== "object") return EMPTY;
    const { visits } = raw as { visits?: unknown };
    const cleanVisits: FrequentState["visits"] = {};
    if (visits && typeof visits === "object") {
      for (const [id, days] of Object.entries(visits as Record<string, unknown>)) {
        if (!days || typeof days !== "object") continue;
        const kept = Object.fromEntries(Object.entries(days as Record<string, unknown>).filter(([day, count]) => /^\d+$/.test(day) && typeof count === "number" && count > 0)) as Record<string, number>;
        if (Object.keys(kept).length) cleanVisits[id] = kept;
      }
    }
    return { visits: cleanVisits };
  } catch {
    return EMPTY;
  }
}

// ── this browser's copy ──────────────────────────────────────────────────────────────────

const KEY = "daedalus.nav.frequent";
let current: FrequentState | null = null;
const listeners = new Set<() => void>();

function storage(): Storage | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

export function readFrequent(): FrequentState {
  if (!current) current = parse(storage()?.getItem(KEY) ?? null);
  return current;
}

function write(next: FrequentState): void {
  current = next;
  try {
    storage()?.setItem(KEY, JSON.stringify(next));
  } catch {
    // A private window or a full quota: the launcher keeps working on this tab's copy.
  }
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  // Another tab of the app pinned something: this one follows without a reload.
  const other = (e: StorageEvent) => {
    if (e.key !== KEY) return;
    current = parse(e.newValue);
    listener();
  };
  window.addEventListener("storage", other);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", other);
  };
}

export function useFrequentState(): FrequentState {
  return useSyncExternalStore(subscribe, readFrequent, readFrequent);
}

/** Counts the place the route stands on once per arrival, and only when `known` says it is a
 *  destination the launcher offers: a chat or a project is not a tile. */
export function useRecordVisit(id: string, known: boolean): void {
  // `known` turns true late for a page, once the page list arrives; remembering the last id counted
  // keeps that second run from counting the same arrival twice.
  const counted = useRef("");
  useEffect(() => {
    if (!known) {
      counted.current = "";
      return;
    }
    if (counted.current === id) return;
    counted.current = id;
    write(recordVisit(readFrequent(), id, Date.now()));
  }, [id, known]);
}
