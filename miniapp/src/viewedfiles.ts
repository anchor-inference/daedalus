// What a reader has marked as viewed in a change set, and how they like their diff laid out. Both are
// that reader's habit rather than the change's state, so they stay in this browser and nowhere else;
// a page that cannot reach storage (a private window, blocked site data) simply starts empty.

import type { DiffMode } from "./diffpane";

const viewedKey = (id: string) => `daedalus.changes.viewed.${id}`;
const MODE = "daedalus.changes.mode";

export function readViewed(id: string): Set<string> {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(viewedKey(id)) ?? "[]");
    return new Set(Array.isArray(parsed) ? parsed.filter((x): x is string => typeof x === "string") : []);
  } catch {
    return new Set();
  }
}

export function writeViewed(id: string, viewed: ReadonlySet<string>): void {
  try {
    if (viewed.size) localStorage.setItem(viewedKey(id), JSON.stringify([...viewed]));
    else localStorage.removeItem(viewedKey(id));
  } catch {
    /* the marks last until the page closes */
  }
}

export function readMode(): DiffMode {
  try {
    return localStorage.getItem(MODE) === "split" ? "split" : "unified";
  } catch {
    return "unified";
  }
}

export function writeMode(mode: DiffMode): void {
  try {
    localStorage.setItem(MODE, mode);
  } catch {
    /* the choice lasts until the page closes */
  }
}
