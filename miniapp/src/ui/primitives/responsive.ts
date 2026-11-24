// Where a floating primitive stops floating and becomes a bottom sheet.
//
// One rule for every popover and menu: below 1024 px of window it is a sheet, and so it is inside a
// container narrower than the box it would open. The second half is the Chrome side panel's and Quick
// Chat's: a 336 px model list anchored in a 320 px column has nowhere to stand, whatever the window.

import { useSyncExternalStore } from "react";

/** The phone's breakpoint, the same 1024 the stylesheets' phone block uses. */
export const SHEET_BELOW = 1024;

/** A point or a text range to hang a popup from, where there is no element to hang it from. */
export type VirtualAnchor = {
  getBoundingClientRect: () => { x: number; y: number; width: number; height: number; top: number; left: number; right: number; bottom: number };
  contextElement?: Element;
};

export type Anchor = HTMLElement | VirtualAnchor;

export function anchorElement(anchor: Anchor | null | undefined): Element | null {
  if (!anchor) return null;
  if (anchor instanceof Element) return anchor;
  return anchor.contextElement ?? null;
}

/** Whether a popup `width` wide, anchored at `anchor`, should be drawn as a sheet. The nearest
    container is the nearest ancestor that declares a `container-type`: those are the columns whose
    own width the layout answers to (the panel, the composer, the browser pane). */
export function sheetForm(anchor: Anchor | null | undefined, sheetBelow = SHEET_BELOW, width?: number): boolean {
  const el = anchorElement(anchor);
  const view = el?.ownerDocument.defaultView ?? window;
  if (view.innerWidth < sheetBelow) return true;
  if (!width || !el) return false;
  for (let at = el.parentElement; at; at = at.parentElement) {
    const type = view.getComputedStyle(at).containerType;
    if (type && type !== "normal") return at.getBoundingClientRect().width < width;
  }
  return false;
}

let widthSeen = typeof window === "undefined" ? 0 : window.innerWidth;
const listeners = new Set<() => void>();
function onResize() {
  if (window.innerWidth === widthSeen) return;
  widthSeen = window.innerWidth;
  listeners.forEach((listener) => listener());
}

/** `sheetForm`, kept current while the window is resized. */
export function useSheetForm(anchor: Anchor | null | undefined, sheetBelow = SHEET_BELOW, width?: number): boolean {
  useSyncExternalStore(
    (listener) => {
      if (listeners.size === 0) window.addEventListener("resize", onResize);
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
        if (listeners.size === 0) window.removeEventListener("resize", onResize);
      };
    },
    () => widthSeen,
    () => 0,
  );
  return sheetForm(anchor, sheetBelow, width);
}
