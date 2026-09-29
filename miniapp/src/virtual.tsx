// A windowed list for the conversation: only the turns near the viewport are in the DOM, the rest
// are two spacers of the right height.
//
// Turns have no common height — a one-line answer and a forty-step run are the same list — so the
// heights are measured as they render and remembered by key; a turn that has never been on screen
// is assumed to be `estimate` tall. Two things keep that honest: the spacers are recomputed from
// the measurements on every layout, and the item the reader is looking at is held in place when a
// guess above it turns out to be wrong.
//
// Below `threshold` items nothing is windowed and the children are rendered exactly as they were,
// with no wrapper: a short conversation is not worth a scroll anchor.

import { Fragment, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ReactNode, RefObject } from "react";

/** The nearest ancestor that really scrolls: the sidebar's body in the shell, the screen on a phone.
 *
 *  Nearest *scrolling*, not nearest scrollable: the sidebar's column holds a screen that is allowed
 *  to scroll and does not, inside a body that does. Taking the first ``overflow: auto`` ancestor
 *  there gives an element whose scrollTop is always zero, and a window that never moves with the
 *  reader. */
export function scrollParent(el: HTMLElement | null): HTMLElement | null {
  let candidate: HTMLElement | null = null;
  for (let p = el?.parentElement ?? null; p; p = p.parentElement) {
    const overflow = getComputedStyle(p).overflowY;
    if (overflow !== "auto" && overflow !== "scroll") continue;
    candidate ??= p;
    if (p.scrollHeight > p.clientHeight + 4) return p;
  }
  return candidate ?? ((document.scrollingElement as HTMLElement | null) ?? null);
}

/** Whether a conversation stays pinned to its end after a scroll event.
 *
 *  The end is taken again whenever the reader is near it. It is let go only when the reader moved up,
 *  or has a finger or a wheel on the list. A scroll that went down or nowhere is the pin's own: the
 *  event arrives after the assignment, and a long history that was still taking its height in between
 *  — an orchestrator's chat, windowed, measuring its turns — read as the reader leaving the end, so it
 *  opened in the middle, every time, with nothing left pinning it. */
export function stillAtEnd({ pinned, gap, top, lastTop, hand }: { pinned: boolean; gap: number; top: number; lastTop: number; hand: boolean }): boolean {
  if (gap < 48) return true;
  return pinned && !hand && top >= lastTop - 1;
}

/** The keys that scroll a list, which the reader drives it with as they would with a wheel. */
export const SCROLL_KEYS = new Set(["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "]);

/** A line the reader opened or closed in a conversation, where it was below the top of the screen,
 *  and until when it is held there. */
export type OpenedLine = { el: Element; top: number; until: number };

/** How long after a click the list's growth is the reader's own doing and not new output. Long enough
 *  for an opened step to render and its code to take its height; short enough that output arriving
 *  while the reader sits at the end is followed again at once. */
const OPENED_HOLD_MS = 800;

/** What a click or a key press toggled inside the conversation, if it toggled anything. The steps, the
 *  line of a turn's work and a system note say whether they are open (`aria-expanded`); the rows of
 *  a turn's thinking and prose are only an `.act`. A link, a menu item or a retry is not held: the
 *  ones meant to take the reader to the end say so themselves. */
export function openedLine(target: EventTarget | null, host: HTMLElement, now = performance.now()): OpenedLine | null {
  if (!(target instanceof Element)) return null;
  const el = target.closest("[aria-expanded], summary, .act");
  if (!el || !host.contains(el)) return null;
  return { el, top: el.getBoundingClientRect().top - host.getBoundingClientRect().top, until: now + OPENED_HOLD_MS };
}

/** Puts a line the reader just opened back where it was on the screen. False once the moment has
 *  passed or the line is gone, and the list is then free to follow its end again. */
export function keepOpened(host: HTMLElement, line: OpenedLine, now = performance.now()): boolean {
  if (now > line.until || !line.el.isConnected) return false;
  const moved = line.el.getBoundingClientRect().top - host.getBoundingClientRect().top - line.top;
  if (Math.abs(moved) >= 1) host.scrollTop += moved;
  return true;
}

/** The first rendered item that reaches into the screen, and how far below the screen's top it starts;
 *  null when none does. A jump of a screen or more (a long wheel turn, a link, find in page) fires its
 *  scroll event while the items of the old place are still the rendered ones, all of them off the
 *  screen. Taking the first of those anyway made an item below the screen the anchor, and opening a
 *  turn's steps above it then "held" that item by throwing the reader up by the steps' whole height. */
function firstInView(host: HTMLElement): { key: string; top: number } | null {
  const box = host.getBoundingClientRect();
  for (const slot of host.querySelectorAll<HTMLElement>("[data-slot]")) {
    const r = slot.getBoundingClientRect();
    if (r.bottom <= box.top) continue;
    return r.top < box.bottom ? { key: slot.dataset.slot!, top: r.top - box.top } : null;
  }
  return null;
}

export type WindowedProps = {
  /** One stable key per item, in order. */
  keys: string[];
  render: (index: number) => ReactNode;
  /** The element that scrolls. */
  scroller: RefObject<HTMLElement | null>;
  /** Height assumed for an item that has not been measured yet. */
  estimate?: number;
  /** How much above and below the viewport is rendered anyway, in pixels. */
  overscan?: number;
  /** The gap the list's own layout puts between items, so the spacers land where the items would. */
  gap?: number;
  threshold?: number;
  /** True while the screen is pinned to the bottom: the anchor must not fight the pin. */
  pinned?: () => boolean;
  /** True while the reader has a finger or a wheel on the list: the anchor must not fight them either. */
  dragging?: () => boolean;
  /** The first item is in the window: whatever comes before it, if anything does, is wanted now. */
  onTop?: () => void;
};

export function Windowed({ keys, render, scroller, estimate = 260, overscan = 900, gap = 14, threshold = 60, pinned, dragging, onTop }: WindowedProps) {
  const sizes = useRef(new Map<string, number>());
  /** The width every stored height was measured at: at another width they describe nothing. */
  const width = useRef(0);
  /** The item the reader is looking at and how far below the top of the screen it starts, so it can be
   *  put back there. Read from the page rather than from the list's own sums: the browser moves the
   *  page for a spacer that changed above the reader by itself where it can, and a correction worked
   *  out from the sums was then applied a second time, throwing the reader down by that much again. */
  const anchor = useRef<{ key: string; top: number } | null>(null);
  /** The heights items had when they were first measured, summed, which is what an unseen item is
   *  guessed at. Not what they measure now: a turn whose steps the reader opened says nothing about
   *  the turns nobody has seen, and counting it moved the spacer above the reader on every click. */
  const firstSeen = useRef({ sum: 0, count: 0 });
  const [, bump] = useState(0);
  const [range, setRange] = useState({ start: 0, end: keys.length });
  const windowed = keys.length > threshold;

  // Where each item starts, from the top of the list, with the gaps counted in. An item nobody has
  // seen yet is assumed to be as tall as the ones that have been measured, so the list does not
  // grow under the reader as they scroll into it.
  const offsets = useRef<number[]>([]);
  if (windowed) {
    const guess = firstSeen.current.count ? firstSeen.current.sum / firstSeen.current.count : estimate;
    const out = new Array<number>(keys.length + 1);
    out[0] = 0;
    for (let i = 0; i < keys.length; i++) out[i + 1] = out[i] + (sizes.current.get(keys[i]) ?? guess) + gap;
    offsets.current = out;
  }

  const recompute = useCallback(() => {
    const host = scroller.current;
    const off = offsets.current;
    if (!host || off.length !== keys.length + 1) return;
    const top = host.scrollTop - overscan;
    const bottom = host.scrollTop + host.clientHeight + overscan;
    let start = 0;
    while (start < keys.length && off[start + 1] < top) start++;
    let end = start;
    while (end < keys.length && off[end] < bottom) end++;
    end = Math.max(end, start + 1);
    // The oldest item the list holds is on screen, so the page before it is what the reader is
    // reaching for. Asking on the rendered range rather than on a distance in pixels is what makes
    // this reliable: a re-measured guess above the reader moves the pixels and does not move this.
    if (start === 0) onTop?.();
    setRange((r) => (r.start === start && r.end === end ? r : { start, end }));
  }, [keys.length, overscan, scroller, onTop]);

  // A new list (a message arrived, an older page was put in front) moves every offset below it.
  useEffect(() => {
    if (windowed) recompute();
    else setRange((r) => (r.start === 0 && r.end === keys.length ? r : { start: 0, end: keys.length }));
  }, [keys, windowed, recompute]);

  useEffect(() => {
    const host = scroller.current;
    if (!host || !windowed) return;
    let frame = 0;
    const on = () => {
      // What the reader is looking at, so neither a re-measured guess above it nor a page of older
      // messages put in front of it moves the page under them.
      anchor.current = firstInView(host);
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        recompute();
      });
    };
    host.addEventListener("scroll", on, { passive: true });
    return () => {
      host.removeEventListener("scroll", on);
      if (frame) cancelAnimationFrame(frame);
    };
  }, [scroller, windowed, recompute]);

  // Measure what is on screen, and hold the reader's place if the spacers moved under them.
  useLayoutEffect(() => {
    const host = scroller.current;
    if (!host || !windowed) return;
    // A turn is as tall as the list is wide. A rotation, a pane opening, a desktop window resized:
    // every height remembered — and the average the unmeasured ones are guessed at — describes a
    // width that is gone, so the spacers would put the reader screens away from where they were.
    const w = host.clientWidth;
    if (w && width.current && w !== width.current) {
      sizes.current.clear();
      firstSeen.current = { sum: 0, count: 0 };
      anchor.current = null;
    }
    if (w) width.current = w;
    let dirty = false;
    for (const slot of host.querySelectorAll<HTMLElement>("[data-slot]")) {
      const key = slot.dataset.slot!;
      const h = slot.offsetHeight;
      if (h > 0 && sizes.current.get(key) !== h) {
        if (!sizes.current.has(key)) firstSeen.current = { sum: firstSeen.current.sum + h, count: firstSeen.current.count + 1 };
        sizes.current.set(key, h);
        dirty = true;
      }
    }
    const held = anchor.current;
    // While a finger or a wheel is on the list the reader is driving it; adding to `scrollTop` under
    // them is felt as the list pulling back, and it is what kept a flick to the top from arriving.
    if (held && !pinned?.() && !dragging?.()) {
      const slot = host.querySelector<HTMLElement>(`[data-slot="${CSS.escape(held.key)}"]`);
      if (slot) {
        const moved = slot.getBoundingClientRect().top - host.getBoundingClientRect().top - held.top;
        if (Math.abs(moved) >= 1) host.scrollTop += moved;
        anchor.current = { key: held.key, top: slot.getBoundingClientRect().top - host.getBoundingClientRect().top };
      } else anchor.current = null;
    }
    // After a jump the items of the new place are rendered only now, and nothing on screen was held:
    // take the one there, so that their heights being measured next does not move the page under the
    // reader.
    if (!anchor.current) anchor.current = firstInView(host);
    if (dirty) bump((n) => n + 1);
  });

  // Images and code blocks take their height after the first layout; the spacers follow them.
  useEffect(() => {
    const host = scroller.current;
    if (!host || !windowed || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => bump((n) => n + 1));
    for (const slot of host.querySelectorAll<HTMLElement>("[data-slot]")) ro.observe(slot);
    return () => ro.disconnect();
  }, [scroller, windowed, range]);

  if (!windowed) return <>{keys.map((_, i) => <Fragment key={keys[i]}>{render(i)}</Fragment>)}</>;

  const off = offsets.current;
  const total = off[keys.length] - gap;
  const start = Math.min(range.start, Math.max(0, keys.length - 1));
  const end = Math.min(Math.max(range.end, start + 1), keys.length);
  const padTop = start > 0 ? off[start] - gap : 0;
  const padBottom = end < keys.length ? total - off[end] : 0;
  const slots: ReactNode[] = [];
  for (let i = start; i < end; i++) {
    slots.push(
      <div key={keys[i]} data-slot={keys[i]} className="turn-slot">
        {render(i)}
      </div>,
    );
  }
  return (
    <>
      {padTop > 0 && <div style={{ height: padTop }} aria-hidden />}
      {slots}
      {padBottom > 0 && <div style={{ height: padBottom }} aria-hidden />}
    </>
  );
}

// A windowed list for rows that are already inside something else: the sessions in a folder, where
// the folder's own header, its root line and its empty state are rendered by the caller and only
// the rows between them are windowed.
//
// Nothing is plumbed through the shell for this. The rows are found in the host by the selector the
// caller gives, the element that scrolls is found by walking up from the host, and the position of
// the list is read from the first row that is actually on screen rather than assumed — so a folder
// below another one whose window just changed is right on the next frame either way.
//
// Heights are measured as rows render and remembered by key; a row nobody has seen is as tall as the
// average of the ones that have been. The row the reader has focused is kept rendered wherever it
// is, because a focused element that unmounts takes the focus to the body with it.

export type WindowedRowsProps = {
  /** One stable key per row, in order. */
  keys: string[];
  render: (index: number) => ReactNode;
  /** The element the rows are rendered into, and how to find them in it. */
  host: RefObject<HTMLElement | null>;
  rowSelector: string;
  /** Height assumed for a row nobody has measured yet. */
  estimate?: number;
  /** How much above and below the viewport is rendered anyway, in pixels. */
  overscan?: number;
  /** Below this many rows nothing is windowed and the rows are rendered exactly as they were. */
  threshold?: number;
};

export function WindowedRows({ keys, render, host, rowSelector, estimate = 64, overscan = 400, threshold = 24 }: WindowedRowsProps) {
  const sizes = useRef(new Map<string, number>());
  const scroller = useRef<HTMLElement | null>(null);
  /** The row the reader is on, so the window never unmounts it from under them. */
  const focused = useRef(-1);
  const [range, setRange] = useState({ start: 0, end: keys.length });
  const held = useRef(range);
  held.current = range;
  const windowed = keys.length > threshold;

  /** Every row's height: measured where it has been on screen, and the estimate where it has not.
   *  A fixed estimate rather than a running average of what has been seen: these rows are one or two
   *  lines and nothing else, so the estimate is right to a pixel or two, and a guess that does not
   *  move is a spacer that does not move the list under the reader as they scroll into it. */
  const heights = useCallback(() => keys.map((key) => sizes.current.get(key) ?? estimate), [keys, estimate]);

  /** The element that scrolls, looked up again while it is not one.
   *
   *  The host is the caller's own element and its ref is attached after this component's effects
   *  have run, so the first look-up finds nothing; and the sidebar's body only starts scrolling once
   *  there are rows in it. Both are answered by asking again rather than by assuming. */
  const resolve = useCallback(() => {
    const found = scroller.current;
    if (found && found.isConnected && found.scrollHeight > found.clientHeight + 4) return found;
    scroller.current = scrollParent(host.current) ?? found;
    return scroller.current;
  }, [host]);

  const recompute = useCallback(() => {
    const scroll = resolve();
    const section = host.current;
    if (!scroll || !section || !windowed) return;
    const rows = section.querySelectorAll<HTMLElement>(rowSelector);
    if (rows.length === 0) return;
    const hs = heights();
    // Where row zero would be, in the scroller's own coordinates, taken from a row that is really
    // there: the rows above it are as tall as they have been measured to be.
    const base = scroll.getBoundingClientRect().top - scroll.scrollTop;
    let above = 0;
    for (let i = 0; i < held.current.start && i < hs.length; i++) above += hs[i];
    const listTop = rows[0].getBoundingClientRect().top - base - above;
    const viewTop = scroll.scrollTop - overscan;
    const viewBottom = scroll.scrollTop + scroll.clientHeight + overscan;
    let start = 0;
    let y = listTop;
    while (start < keys.length - 1 && y + hs[start] < viewTop) {
      y += hs[start];
      start += 1;
    }
    let end = start;
    let bottom = y;
    while (end < keys.length && bottom < viewBottom) {
      bottom += hs[end];
      end += 1;
    }
    end = Math.max(end, start + 1);
    if (focused.current >= 0 && focused.current < keys.length) {
      start = Math.min(start, focused.current);
      end = Math.max(end, focused.current + 1);
    }
    setRange((r) => (r.start === start && r.end === end ? r : { start, end }));
  }, [keys.length, heights, host, overscan, resolve, rowSelector, windowed]);

  useEffect(() => {
    if (windowed) recompute();
    else setRange((r) => (r.start === 0 && r.end === keys.length ? r : { start: 0, end: keys.length }));
  }, [keys, windowed, recompute]);

  useEffect(() => {
    const scroll = resolve();
    if (!scroll || !windowed) return;
    const target: EventTarget = scroll === document.scrollingElement ? window : scroll;
    let frame = 0;
    const on = () => {
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        recompute();
      });
    };
    target.addEventListener("scroll", on, { passive: true });
    window.addEventListener("resize", on);
    return () => {
      target.removeEventListener("scroll", on);
      window.removeEventListener("resize", on);
      if (frame) cancelAnimationFrame(frame);
    };
  }, [keys.length, windowed, resolve, recompute]);

  // Measure what is on screen. A row's height is taken from where the next one starts, so the margin
  // between them is part of it and the spacers keep the list exactly as tall as it was.
  useLayoutEffect(() => {
    const section = host.current;
    if (!section || !windowed) return;
    const rows = [...section.querySelectorAll<HTMLElement>(rowSelector)];
    let dirty = false;
    for (let i = 0; i < rows.length; i++) {
      const key = keys[held.current.start + i];
      if (key === undefined) break;
      const rect = rows[i].getBoundingClientRect();
      const next = rows[i + 1];
      const h = Math.round(next ? next.getBoundingClientRect().top - rect.top : rect.height);
      if (h > 0 && sizes.current.get(key) !== h) {
        sizes.current.set(key, h);
        dirty = true;
      }
    }
    if (dirty) recompute();
  });

  // Where the reader's focus is, so the window keeps that row however far they scroll away from it.
  useEffect(() => {
    const section = host.current;
    if (!section || !windowed) return;
    const on = (e: FocusEvent) => {
      const rows = [...section.querySelectorAll<HTMLElement>(rowSelector)];
      const at = rows.findIndex((row) => row === e.target || row.contains(e.target as Node));
      focused.current = at < 0 ? -1 : held.current.start + at;
    };
    section.addEventListener("focusin", on);
    return () => section.removeEventListener("focusin", on);
  }, [host, rowSelector, windowed]);

  if (!windowed) return <>{keys.map((_, i) => <Fragment key={keys[i]}>{render(i)}</Fragment>)}</>;

  const hs = heights();
  const start = Math.min(range.start, Math.max(0, keys.length - 1));
  const end = Math.min(Math.max(range.end, start + 1), keys.length);
  let padTop = 0;
  for (let i = 0; i < start; i++) padTop += hs[i];
  let padBottom = 0;
  for (let i = end; i < keys.length; i++) padBottom += hs[i];
  const slots: ReactNode[] = [];
  for (let i = start; i < end; i++) slots.push(<Fragment key={keys[i]}>{render(i)}</Fragment>);
  return (
    <>
      {padTop > 0 && <div style={{ height: Math.round(padTop) }} aria-hidden />}
      {slots}
      {padBottom > 0 && <div style={{ height: Math.round(padBottom) }} aria-hidden />}
    </>
  );
}
