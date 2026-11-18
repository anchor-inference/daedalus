// Where a file dragged in from the desktop may land: on an element that registered for it (the composer,
// a question's card), and nowhere else.
//
// The conversation once caught a file anywhere in it: the whole window dimmed under a blurred overlay,
// and the overlay was a counter of dragenter and dragleave events kept in the session's React state.
// Every element the pointer crossed changed that state, and each change rendered the whole session,
// transcript included, while the blur repainted the transcript under it on every frame — the drag
// stuttered. The counter also lost its balance (a question card that took the drop stopped the event
// before the chat could reset it, and a drag cancelled outside the window never left), so the overlay
// stayed up. And because it covered everything, a file meant for a question the agent had asked went
// to the composer.
//
// So no React state moves during a drag. One listener on the window asks, at every dragover, which
// registered element is under the pointer — the innermost, so a question card inside a panel wins over
// anything around it — and marks that element with an attribute the style sheet draws. There is no
// count to drift: the mark follows the pointer, and it clears on drop, on Escape, on leaving the
// window, and, should the browser say nothing at all, when the dragovers stop coming.

import { useEffect, useRef, type RefObject } from "react";

const TARGET = "data-drop-target";
const OVER = "data-drop-over";

/** How long without a dragover before the mark is taken down. Browsers repeat dragover while the pointer
 *  rests over the page (the specification allows up to 550 ms between them), so a longer silence means
 *  the drag ended somewhere the page was not told about. */
export const QUIET_MS = 800;
/** After a dragleave, how soon the mark goes unless a dragover arrives: a leave inside the page is
 *  followed at once by the next element's dragover, a leave out of the window or a cancelled drag is not. */
export const LEAVE_MS = 150;

type Take = (files: File[]) => void;

const targets = new Map<Element, { current: Take | null }>();
let over: Element | null = null;
let timer: ReturnType<typeof setTimeout> | undefined;
let installed = false;

/** Whether a drag carries files from outside the page. A drag of a chat row or a task is not one. */
export function carriesFiles(data: DataTransfer | null | undefined): boolean {
  return !!data && Array.from(data.types ?? []).includes("Files");
}

/** The innermost registered element at a node, or null where nothing takes files. */
export function targetAt(node: EventTarget | null): Element | null {
  let at: Element | null = node instanceof Element ? node : node instanceof Node ? node.parentElement : null;
  while (at) {
    const found: Element | null = at.closest(`[${TARGET}]`);
    if (!found) return null;
    if (targets.get(found)?.current) return found;
    at = found.parentElement;
  }
  return null;
}

/** The element marked as the drop's destination now, for tests. */
export function marked(): Element | null {
  return over;
}

function mark(next: Element | null) {
  if (next === over) return;
  over?.removeAttribute(OVER);
  next?.setAttribute(OVER, "");
  over = next;
}

function wait(ms: number) {
  if (timer !== undefined) clearTimeout(timer);
  timer = setTimeout(clear, ms);
}

/** Take the mark down. */
export function clear() {
  if (timer !== undefined) clearTimeout(timer);
  timer = undefined;
  mark(null);
}

// The window hears these after React's own handlers have run (React listens on its root, below the
// window), so a drop zone of its own — the notes panel, the new-chat box — has already called
// preventDefault, and is left to do its work.
function onDragOver(e: DragEvent) {
  if (!carriesFiles(e.dataTransfer)) return;
  wait(QUIET_MS);
  if (e.defaultPrevented) {
    mark(null);
    return;
  }
  const target = targetAt(e.target);
  mark(target);
  // Taken everywhere, so that a file let go of outside every target is not opened by the browser in
  // place of the app; "none" draws the refusing cursor there, and the drop then never happens.
  e.preventDefault();
  if (e.dataTransfer) e.dataTransfer.dropEffect = target ? "copy" : "none";
}

function onDragLeave(e: DragEvent) {
  if (carriesFiles(e.dataTransfer) && over) wait(LEAVE_MS);
}

function onDrop(e: DragEvent) {
  if (!carriesFiles(e.dataTransfer)) return;
  const target = e.defaultPrevented ? null : targetAt(e.target);
  clear();
  if (e.defaultPrevented) return;
  e.preventDefault();
  const files = Array.from(e.dataTransfer?.files ?? []);
  if (target && files.length) targets.get(target)?.current?.(files);
}

function onKey(e: KeyboardEvent) {
  if (e.key === "Escape" && over) clear();
}

/** Listen on the window, once. Called by the first registration, and by the app at start so that a file
 *  let go of on a screen without any target is not opened in place of the app either. */
export function installFileDrop() {
  if (installed || typeof window === "undefined") return;
  installed = true;
  window.addEventListener("dragover", onDragOver);
  window.addEventListener("dragleave", onDragLeave);
  window.addEventListener("drop", onDrop);
  window.addEventListener("dragend", clear);
  window.addEventListener("keydown", onKey);
}

/** Make the element in `ref` a place files can be dropped on; `take` gets them. Null takes nothing,
 *  and the element is then no target at all. The registration follows `take` being null or not, so an
 *  element that is swapped for another must pass a different condition with it (a card drawn twice). */
export function useDropTarget(ref: RefObject<Element | null>, take: Take | null) {
  const latest = useRef(take);
  latest.current = take;
  const on = !!take;
  useEffect(() => {
    const el = ref.current;
    if (!el || !on) return;
    installFileDrop();
    targets.set(el, latest);
    el.setAttribute(TARGET, "");
    return () => {
      if (targets.get(el) === latest) targets.delete(el);
      el.removeAttribute(TARGET);
      if (over === el) mark(null);
    };
  }, [ref, on]);
}
