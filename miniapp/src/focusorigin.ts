// Marks the element the Tab key moved the focus to (`data-tabbed`, until it loses the focus), so the
// stylesheet can tell a region the reader tabbed to from one a script or the browser focused. The
// focus ring is drawn on controls only (ui/parts/base.css): a container — the conversation's scroller,
// the panel's body, a sheet — that took the focus after a message was sent with Enter counted as
// keyboard focus and drew a violet ring around the whole conversation. A reader who tabs to such a
// region on purpose (a scroller with nothing focusable in it is in the tab order) still sees where
// the focus went, by this mark.

/** Listens on `doc` for good; the app calls it once at start. Returns the way to stop, for a test. */
export function installFocusOrigin(doc: Document = document): () => void {
  let tabbing = false;
  let clear = 0;
  const onKey = (e: KeyboardEvent) => {
    tabbing = e.key === "Tab";
    // The focus moves as the key's own default action, before any timer runs; a Tab that moved
    // nothing (a field that took it to accept a suggestion) must not mark a later focus from a script.
    window.clearTimeout(clear);
    if (tabbing) clear = window.setTimeout(() => { tabbing = false; }, 0);
  };
  const onPointer = () => { tabbing = false; };
  const onFocus = (e: FocusEvent) => {
    if (tabbing && e.target instanceof HTMLElement) e.target.setAttribute("data-tabbed", "");
    tabbing = false;
  };
  const onBlur = (e: FocusEvent) => {
    if (e.target instanceof HTMLElement) e.target.removeAttribute("data-tabbed");
  };
  doc.addEventListener("keydown", onKey, true);
  doc.addEventListener("pointerdown", onPointer, true);
  doc.addEventListener("focusin", onFocus, true);
  doc.addEventListener("focusout", onBlur, true);
  return () => {
    window.clearTimeout(clear);
    doc.removeEventListener("keydown", onKey, true);
    doc.removeEventListener("pointerdown", onPointer, true);
    doc.removeEventListener("focusin", onFocus, true);
    doc.removeEventListener("focusout", onBlur, true);
  };
}
