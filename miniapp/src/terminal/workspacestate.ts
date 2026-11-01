// The Terminals workspace as data: which terminal sits in which of the four places of the grid, how
// many of them are shown, where the splitters are, and which place has the keyboard. Pure, so the
// edge cases (a swap onto itself, a clamp, going 4 → 1 → 4) are tested without a browser.
//
// There are always four places. A layout shows the first one, two or four of them; the others keep
// their terminals, so going from four panes to one and back loses nothing. A terminal is in at most
// one place: opening one that is already there focuses its place instead of showing it twice.

export const SLOTS = 4;
export type Layout = 1 | 2 | 4;
export const LAYOUTS: Layout[] = [1, 2, 4];

/** A splitter never leaves a pane less than a fifth of the grid: below that a terminal is a strip of
 *  a few columns, and every one of those columns is a RESIZE the program redraws for. */
export const MIN_FRACTION = 0.2;
export const MAX_FRACTION = 0.8;

export type Workspace = {
  layout: Layout;
  /** The terminal in each of the four places, or null for an empty one. */
  panes: (string | null)[];
  /** The left column's share of the width, and the top row's share of the height (the 2 × 2 layout). */
  col: number;
  row: number;
  /** The place that has the keyboard: always one of the shown places. */
  active: number;
  /** A place drawn over the whole grid for a while ("maximise"), or null. */
  zoom: number | null;
};

export const EMPTY: Workspace = { layout: 1, panes: [null, null, null, null], col: 0.5, row: 0.5, active: 0, zoom: null };

export function clampFraction(value: number): number {
  if (!Number.isFinite(value)) return 0.5;
  return Math.min(MAX_FRACTION, Math.max(MIN_FRACTION, value));
}

/** The places a layout shows, in reading order. */
export function shownSlots(ws: Workspace): number[] {
  if (ws.zoom !== null) return [ws.zoom];
  return Array.from({ length: ws.layout }, (_, i) => i);
}

export function isShown(ws: Workspace, slot: number): boolean {
  return shownSlots(ws).includes(slot);
}

export function activeId(ws: Workspace): string | null {
  return ws.panes[ws.active] ?? null;
}

/** Whether anything is set up: a terminal in a place the layout shows. */
export function hasPanes(ws: Workspace): boolean {
  return Array.from({ length: ws.layout }, (_, i) => ws.panes[i]).some(Boolean);
}

function layoutOf(value: unknown): Layout {
  const n = Number(value);
  // Three panes are drawn as four: the 2 × 2 grid with one place left empty.
  if (n === 3) return 4;
  return (LAYOUTS as number[]).includes(n) ? (n as Layout) : 1;
}

/**
 * A workspace from whatever storage or an address held. Anything malformed falls back to its default
 * rather than failing the page: a reload must always open a workspace, even after the shape changed.
 */
export function normalise(raw: unknown): Workspace {
  const source = (raw && typeof raw === "object" ? raw : {}) as Partial<Record<keyof Workspace, unknown>>;
  const layout = layoutOf(source.layout ?? 1);
  const panes: (string | null)[] = [null, null, null, null];
  const seen = new Set<string>();
  if (Array.isArray(source.panes)) {
    source.panes.slice(0, SLOTS).forEach((id, i) => {
      const clean = typeof id === "string" ? id.trim() : "";
      if (clean && !seen.has(clean)) {
        seen.add(clean);
        panes[i] = clean;
      }
    });
  }
  const active = Number.isInteger(source.active) && (source.active as number) >= 0 && (source.active as number) < layout ? (source.active as number) : 0;
  const zoom = Number.isInteger(source.zoom) && (source.zoom as number) >= 0 && (source.zoom as number) < layout ? (source.zoom as number) : null;
  return { layout, panes, col: clampFraction(Number(source.col ?? 0.5)), row: clampFraction(Number(source.row ?? 0.5)), active, zoom };
}

export function setLayout(ws: Workspace, layout: Layout): Workspace {
  return { ...ws, layout, active: ws.active < layout ? ws.active : 0, zoom: null };
}

export function focus(ws: Workspace, slot: number): Workspace {
  if (!isShown(ws, slot) && ws.zoom === null) return ws;
  // Focusing another place while one is maximised brings the grid back: the person asked for that pane.
  if (ws.zoom !== null && slot !== ws.zoom) return slot < ws.layout ? { ...ws, zoom: null, active: slot } : ws;
  return ws.active === slot ? ws : { ...ws, active: slot };
}

/** Swaps the terminals of two places; the keyboard follows the pane that moved. Onto itself, or from
 *  or to a place that does not exist, nothing changes. */
export function swap(ws: Workspace, from: number, to: number): Workspace {
  if (from === to || from < 0 || to < 0 || from >= SLOTS || to >= SLOTS) return ws;
  const panes = [...ws.panes];
  [panes[from], panes[to]] = [panes[to], panes[from]];
  const active = ws.active === from ? to : ws.active === to ? from : ws.active;
  return { ...ws, panes, active };
}

/**
 * Shows a terminal. Already in a shown place: that place is focused. In a hidden place: it trades
 * places with where it lands, so the terminal it replaces is kept hidden rather than dropped. Not
 * there at all: it lands in the active place when that is empty, else in the first empty shown
 * place, else in the active place (as a chat opened from paddock's list lands in the active pane).
 */
export function place(ws: Workspace, id: string): Workspace {
  const at = ws.panes.indexOf(id);
  const unzoomed = ws.zoom !== null && at !== ws.zoom ? { ...ws, zoom: null } : ws;
  if (at >= 0 && at < unzoomed.layout) return focus(unzoomed, at);
  const shown = Array.from({ length: unzoomed.layout }, (_, i) => i);
  const target = !unzoomed.panes[unzoomed.active] ? unzoomed.active : shown.find((i) => !unzoomed.panes[i]) ?? unzoomed.active;
  if (at >= 0) return { ...swap(unzoomed, at, target), active: target };
  const panes = [...unzoomed.panes];
  panes[target] = id;
  return { ...unzoomed, panes, active: target };
}

/** Empties a place. The terminal goes on running, as it does when the dock's tab is closed. */
export function close(ws: Workspace, slot: number): Workspace {
  if (!ws.panes[slot]) return ws;
  const panes = [...ws.panes];
  panes[slot] = null;
  return { ...ws, panes, zoom: ws.zoom === slot ? null : ws.zoom };
}

/** Puts `next` where `previous` was: a restarted terminal comes back under a new id, in the same place. */
export function replace(ws: Workspace, previous: string, next: string): Workspace {
  const at = ws.panes.indexOf(previous);
  if (at < 0) return ws;
  const panes = ws.panes.map((id) => (id === next ? null : id));
  panes[at] = next;
  return { ...ws, panes };
}

export function maximise(ws: Workspace, slot: number): Workspace {
  if (ws.zoom === slot) return { ...ws, zoom: null };
  if (ws.layout === 1 || slot >= ws.layout) return ws;
  return { ...ws, zoom: slot, active: slot };
}

export function setFractions(ws: Workspace, fractions: { col?: number; row?: number }): Workspace {
  const col = fractions.col === undefined ? ws.col : clampFraction(fractions.col);
  const row = fractions.row === undefined ? ws.row : clampFraction(fractions.row);
  return col === ws.col && row === ws.row ? ws : { ...ws, col, row };
}

// ── the address ───────────────────────────────────────────────────────────────────────────────
//
// `/app/terminals/<active>?layout=4&panes=a,b,,d` is the workspace as the page writes it, so a reload
// or a pasted link lands on the same grid. `/app/terminals/<id>` alone, as every other part of the app
// links a terminal, shows that terminal in the stored workspace. `?with=a,b` is the grid address of
// the full-screen view this replaced, and still opens those terminals side by side.

export type Address = { detail: string | null; query: URLSearchParams };

export function fromAddress(stored: Workspace, { detail, query }: Address): Workspace {
  const panes = query.get("panes");
  if (panes !== null) {
    const ws = normalise({ ...stored, layout: query.get("layout") ?? stored.layout, panes: panes.split(",") });
    const at = detail ? ws.panes.indexOf(detail) : -1;
    if (at >= 0 && at < ws.layout) return { ...ws, active: at };
    return detail ? place(ws, detail) : ws;
  }
  const beside = query.get("with");
  if (detail && beside !== null) {
    const ids = [detail, ...beside.split(",")].map((id) => id.trim()).filter(Boolean);
    const unique = ids.filter((id, i) => ids.indexOf(id) === i).slice(0, SLOTS);
    return normalise({ ...stored, layout: unique.length, panes: unique, active: 0, zoom: null });
  }
  return detail ? place(stored, detail) : stored;
}

/** The path segment and the query the page writes for a workspace. */
export function toAddress(ws: Workspace): { detail: string | null; query: Record<string, string> } {
  const used = ws.panes.some(Boolean);
  const query: Record<string, string> = { layout: String(ws.layout) };
  if (used) query.panes = ws.panes.map((id) => id ?? "").join(",").replace(/,+$/, "");
  const detail = activeId(ws);
  if (!detail) query.view = "workspace";
  return { detail, query };
}

// ── storage ───────────────────────────────────────────────────────────────────────────────────

export const STORAGE_KEY = "daedalus.term.workspace";
export const VIEW_KEY = "daedalus.term.view";
export type TerminalsView = "overview" | "workspace";

export function loadWorkspace(): Workspace {
  try {
    const text = localStorage.getItem(STORAGE_KEY);
    return text ? normalise(JSON.parse(text)) : EMPTY;
  } catch {
    return EMPTY;
  }
}

export function saveWorkspace(ws: Workspace): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(ws));
  } catch {
    /* remembered for this page only */
  }
}

export function loadView(): TerminalsView | null {
  try {
    const value = localStorage.getItem(VIEW_KEY);
    return value === "overview" || value === "workspace" ? value : null;
  } catch {
    return null;
  }
}

export function saveView(view: TerminalsView): void {
  try {
    localStorage.setItem(VIEW_KEY, view);
  } catch {
    /* remembered for this page only */
  }
}

/**
 * Which view `/app/terminals` opens on: the Workspace when panes are set up, unless the operator
 * chose the Overview last; the Overview when nothing is set up, whatever was chosen, because an
 * empty grid is not a page anyone meant to come back to.
 */
export function openingView(remembered: TerminalsView | null, ws: Workspace): TerminalsView {
  return hasPanes(ws) && remembered !== "overview" ? "workspace" : "overview";
}
