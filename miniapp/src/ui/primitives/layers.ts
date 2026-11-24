// The stack of temporary layers open over the screen, top last. Escape goes to the top one only,
// whoever drew it: a Base UI popup, or one of the hand-drawn overlays that the screens still own.
//
// Base UI keeps its own idea of nesting, but only for popups declared inside one another in the React
// tree. The app opens a confirmation from inside a sheet through a host at the root, and a screen's
// own overlay can sit over a Base UI menu, so the two trees know nothing of each other: left to
// itself, one Escape closed the confirmation and the sheet under it, and a press inside the
// confirmation read to the sheet as a press outside it. Every primitive therefore registers here,
// hands Escape to this stack, and ignores an outside press while another layer is above it.

import { useEffect, useRef } from "react";

const layers: symbol[] = [];

/** Whether a sheet, confirmation or menu is open over the screen. A screen that focuses its own
    field on mount asks this first: the start screen is a lazy chunk that can arrive after a dialog
    opened, and its composer took the focus out from under the dialog, so the project name typed
    into the new-project sheet went into the hidden composer and Create refused an empty name. */
export function layerOpen(): boolean {
  return layers.length > 0;
}

/** Joins the stack while `active`, and calls `onEscape` when Escape is pressed while this layer is
    the top one. Returns a function that says whether it is the top one right now.

    The key is read in the capture phase. A hand-drawn overlay's root stops events so a click in it
    never reaches the row that opened it, and that stop happens at the portal's container, before
    the document's bubble phase: a listener there never heard the key at all. */
export function useLayer(onEscape: () => void, active = true): () => boolean {
  const callback = useRef(onEscape);
  callback.current = onEscape;
  const me = useRef<symbol | null>(null);
  useEffect(() => {
    if (!active) return;
    const mine = Symbol("layer");
    me.current = mine;
    layers.push(mine);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && layers[layers.length - 1] === mine) {
        e.preventDefault();
        callback.current();
      }
    };
    document.addEventListener("keydown", onKey, { capture: true });
    return () => {
      document.removeEventListener("keydown", onKey, { capture: true });
      const i = layers.indexOf(mine);
      if (i >= 0) layers.splice(i, 1);
      if (me.current === mine) me.current = null;
    };
  }, [active]);
  return () => me.current !== null && layers[layers.length - 1] === me.current;
}

/** The reasons Base UI may give for closing a popup that only the top layer may act on: Escape and
    the Android back gesture belong to the stack above, a press outside means nothing while another
    layer covers this one, and a focus that left for a layer above is not a reason to close. */
const STACKED = new Set(["escape-key", "close-watcher", "outside-press", "focus-out"]);

/** The one `onOpenChange` every primitive gives its Base UI root: it closes only through `onClose`,
    and only when the request is one the top layer may make. */
export function closeHandler(isTop: () => boolean, onClose: () => void, ignore?: (target: Element) => boolean) {
  return (open: boolean, details: { reason: string; event?: Event; cancel: () => void }) => {
    if (open) return;
    // Where the press landed, or for a focus that left, where it went: a press on the control that
    // opened the popup moves the focus there first, and closing on that focus let the control's own
    // click open the popup again.
    const towards = details.reason === "focus-out" ? (details.event as FocusEvent | undefined)?.relatedTarget : details.event?.target;
    const target = towards instanceof Element ? towards : null;
    const away = details.reason === "outside-press" || details.reason === "focus-out";
    // A toast is not a layer: its Undo is pressed with the sheet still open, and that press is not
    // a request to close the sheet.
    const spared = away && target !== null && (target.closest(".toast") !== null || (ignore?.(target) ?? false));
    if (details.reason === "escape-key" || details.reason === "close-watcher" || spared || (STACKED.has(details.reason) && !isTop())) {
      details.cancel();
      return;
    }
    onClose();
  };
}

/** Gives the focus back to `target` when the layer goes, unless something else has taken it.

    The layers own this rather than Base UI: a layer here is unmounted, not closed, and Base UI then
    decided where the focus went in the same moment the command that closed it was giving it away —
    a Rename in a menu put the caret in its field, the menu took it back to the "…", and the field,
    blurred, gave up the rename. Here only a focus that fell to the page is picked up, after the
    command has done its work. */
export function useFocusBack(target: () => HTMLElement | null | undefined) {
  const get = useRef(target);
  get.current = target;
  useEffect(() => () => {
    const back = get.current();
    // A modal layer leaves the page under it inert for a frame or two after it goes, and a focus
    // given then is refused without a word: it is tried until it lands or something else has it.
    // It is tried at once as well: a phone's composer hides its model chip a moment after the model
    // sheet goes unless the focus is already back on it.
    let tries = 0;
    const give = () => {
      const now = document.activeElement;
      if ((now && now !== document.body) || !back?.isConnected) return;
      back.focus({ preventScroll: true });
      if (document.activeElement !== back && ++tries < 10) requestAnimationFrame(give);
    };
    give();
  }, []);
}

/** Keeps Tab inside a hand-drawn dialog. The Base UI ones trap focus themselves. */
export function trapDialogTab(e: React.KeyboardEvent<HTMLElement>) {
  if (e.key !== "Tab") return;
  const items = Array.from(e.currentTarget.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), a[href], [tabindex='0']")).filter((el) => el.getClientRects().length > 0);
  if (!items.length) { e.preventDefault(); return; }
  const index = items.indexOf(document.activeElement as HTMLElement);
  if (e.shiftKey && index <= 0) { e.preventDefault(); items[items.length - 1].focus(); }
  else if (!e.shiftKey && (index < 0 || index === items.length - 1)) { e.preventDefault(); items[0].focus(); }
}
