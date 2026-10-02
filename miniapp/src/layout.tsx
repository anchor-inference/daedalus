// The layout is the operator's, not the session's: which panes are open, how wide they were dragged,
// whether the sidebar is a column or folded away behind the rail. All of it is remembered per
// browser and none of it travels with a session, so leaving for Settings and coming back does not mean reopening anything.

import { useCallback, useRef, useState } from "react";
import { t } from "./i18n";

export function readLayout(key: string): string | null {
  try {
    return localStorage.getItem(`daedalus.session.${key}`);
  } catch {
    return null;
  }
}

export function writeLayout(key: string, value: string): void {
  try {
    localStorage.setItem(`daedalus.session.${key}`, value);
  } catch {
    /* private mode: the layout lasts for the visit */
  }
}

/** A pane width the operator dragged, remembered per browser. */
export function usePaneWidth(key: string, initial: number, min: number, max: number): [number, (w: number) => void] {
  const storageKey = `daedalus.width.${key}`;
  const [width, setWidth] = useState(() => {
    try {
      const v = Number(localStorage.getItem(storageKey));
      return v >= min && v <= max ? v : initial;
    } catch {
      return initial;
    }
  });
  const set = useCallback(
    (w: number) => {
      const clamped = Math.round(Math.min(max, Math.max(min, w)));
      setWidth(clamped);
      try {
        localStorage.setItem(storageKey, String(clamped));
      } catch {
        /* private mode */
      }
    },
    [storageKey, min, max],
  );
  return [width, set];
}

/**
 * What dragging a pane's edge does. The width goes straight onto the page while the pointer moves —
 * a custom property or a style written by `show`, at most once a frame — and reaches React state and
 * storage once, in `commit`, when the pointer lets go.
 *
 * The drag used to set React state on every pointermove: the whole shell re-rendered per event, the
 * width was written to storage per event, and the sidebar's CSS width transition then chased each new
 * value for 180 ms, so the edge lagged behind the pointer and caught up in a glide. It felt jerky and
 * oddly smooth at once. Nothing re-renders during a drag now, and `body.resizing` switches every
 * transition on the panes off until the pointer is released.
 */
export type PaneDrag = {
  /** The pane's width in pixels as the drag begins. */
  begin: () => number;
  /** Show a width while the pointer moves; the owner clamps it and writes it to the page directly. */
  show: (width: number) => void;
  /** The pointer let go: keep the width, clamped the same way. */
  commit: (width: number) => void;
  /** 1 when moving the pointer right widens the pane (a pane on the left), -1 when it narrows it. */
  sign: 1 | -1;
  /** Pixels one arrow key moves the edge. Given, the strip takes focus and the arrow keys. */
  step?: number;
  /** A double click (or Home) gives the pane its default width back. */
  reset?: () => void;
};

/** A drag that writes a pixel width to one property of an element: a custom property the stylesheet
 *  reads (the sidebar's `--sidebar-w`), or `width` itself. */
export function pixelDrag(target: () => HTMLElement | null, property: string, begin: () => number, clamp: (w: number) => number, commit: (w: number) => void, sign: 1 | -1 = 1): PaneDrag {
  return {
    begin,
    show: (w) => target()?.style.setProperty(property, `${clamp(w)}px`),
    commit: (w) => commit(clamp(w)),
    sign,
  };
}

/** The clamp a remembered pane width is held to, rounded to whole pixels. */
export function clampWidth(w: number, min: number, max: number): number {
  return Math.round(Math.min(max, Math.max(min, w)));
}

/** The strip between two panes: drag it to resize (pointer events, so mouse and touch alike). */
export function PaneHandle({ side, drag }: { side: "left" | "right"; drag: PaneDrag }) {
  // The owner's callbacks close over its latest render; the drag reads them through this ref, so a
  // re-render of the owner mid-drag neither restarts the drag nor strands it on stale ones.
  const latest = useRef(drag);
  latest.current = drag;
  const live = useRef<{ pointer: number; x0: number; w0: number; x: number; frame: number } | null>(null);
  const widthAt = (d: { x0: number; w0: number; x: number }) => d.w0 + latest.current.sign * (d.x - d.x0);
  const paint = () => {
    const d = live.current;
    if (!d) return;
    d.frame = 0;
    latest.current.show(widthAt(d));
  };
  const end = (e: React.PointerEvent<HTMLDivElement>) => {
    const d = live.current;
    if (!d || e.pointerId !== d.pointer) return;
    live.current = null;
    if (d.frame) cancelAnimationFrame(d.frame);
    if (e.type === "pointerup") d.x = e.clientX;
    const w = widthAt(d);
    latest.current.show(w);
    latest.current.commit(w);
    document.body.classList.remove("resizing");
    if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId);
  };
  // The keys move the edge the way the pointer would: → and ← by one step in the pointer's direction,
  // Home for the default width. Each press is a whole drag, so it is kept at once.
  const onKey = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const d = latest.current;
    if (!d.step) return;
    if (e.key === "Home" && d.reset) {
      e.preventDefault();
      d.reset();
      return;
    }
    const dir = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (!dir) return;
    e.preventDefault();
    const w = d.begin() + d.sign * dir * d.step * (e.shiftKey ? 4 : 1);
    d.show(w);
    d.commit(w);
  };
  return (
    <div
      className={`pane-handle wide-only ${side}`}
      role="separator"
      aria-orientation="vertical"
      aria-label={t("session.resize")}
      tabIndex={drag.step ? 0 : undefined}
      title={drag.reset ? t("session.resize.hint") : undefined}
      onKeyDown={drag.step ? onKey : undefined}
      onDoubleClick={drag.reset ? () => latest.current.reset?.() : undefined}
      onPointerDown={(e) => {
        if (e.button !== 0) return;
        // No text selection starts under a drag, and the capture keeps the moves coming when the
        // pointer outruns the 7 px strip or crosses an iframe.
        e.preventDefault();
        e.currentTarget.setPointerCapture(e.pointerId);
        live.current = { pointer: e.pointerId, x0: e.clientX, w0: latest.current.begin(), x: e.clientX, frame: 0 };
        document.body.classList.add("resizing");
      }}
      onPointerMove={(e) => {
        const d = live.current;
        if (!d || e.pointerId !== d.pointer) return;
        d.x = e.clientX;
        // Several moves arrive per frame on a fast mouse; the page is written once per frame, with the last.
        if (!d.frame) d.frame = requestAnimationFrame(paint);
      }}
      onPointerUp={end}
      onPointerCancel={end}
      onLostPointerCapture={end}
    />
  );
}

// ── the sidebar: a column beside the rail, or folded away ───────────────────────────────────

const SIDEBAR = "daedalus.sidebar";

/** Below this the column would take a third of the window, so folded is the default there: the rail alone is left. */
export const SIDEBAR_COLUMN_MIN = 1100;

/** Whether the sidebar is folded: what the operator chose, or, having chosen nothing, what the window allows. */
export function sidebarCollapsed(stored: string | null, viewportWidth: number): boolean {
  if (stored === "collapsed") return true;
  if (stored === "open") return false;
  return viewportWidth < SIDEBAR_COLUMN_MIN;
}

export function readSidebar(viewportWidth: number): boolean {
  let stored: string | null = null;
  try {
    stored = localStorage.getItem(SIDEBAR);
  } catch {
    /* private mode */
  }
  return sidebarCollapsed(stored, viewportWidth);
}

export function rememberSidebar(collapsed: boolean): void {
  try {
    localStorage.setItem(SIDEBAR, collapsed ? "collapsed" : "open");
  } catch {
    /* private mode: the choice lasts for the visit */
  }
}
