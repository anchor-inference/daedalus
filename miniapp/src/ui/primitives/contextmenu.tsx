// The right click: the same commands, in the same menu, as the visible "…" beside a row, and a
// short editing menu in a text field. One menu for the whole app, hung from the point pressed: a
// menu per row would have multiplied Base UI's wrappers over the three hundred rows of a long
// sidebar for a menu that is only ever open once.

import { useCallback, useEffect, useMemo, useState, type RefObject } from "react";
import { t } from "../../i18n";
import { copyText } from "../components";
import { Menu, type MenuItem } from "./menu";
import { toast } from "./toast";
import type { VirtualAnchor } from "./responsive";

const EVENT = "daedalus-context-menu";
type Request = { x: number; y: number; items: MenuItem[]; owner: HTMLElement };
type RequestEvent = CustomEvent<Request>;
export function openContextMenu(request: Request) {
  document.dispatchEvent(new CustomEvent(EVENT, { detail:request }));
}

/** The right click invokes the same commands and confirmations as the visible overflow button. */
export function useContextActions(ref: RefObject<HTMLElement | null>, items: MenuItem[], selector?: string) {
  useEffect(() => {
    const owner = selector ? ref.current?.closest<HTMLElement>(selector) : ref.current;
    if (!owner) return;
    const open = (event: MouseEvent | KeyboardEvent) => {
      if (event.defaultPrevented) return;
      if (event instanceof KeyboardEvent && event.key !== "ContextMenu" && !(event.shiftKey && event.key === "F10")) return;
      if ((event.target as HTMLElement).closest("input, textarea, [contenteditable]:not([contenteditable='false'])")) return;
      if (window.getSelection()?.toString()) return;
      const rect = owner.getBoundingClientRect();
      event.preventDefault();
      event.stopPropagation();
      const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      const focus = owner.tabIndex >= 0 ? owner : previous && previous !== document.body ? previous : owner;
      openContextMenu({ x:event instanceof MouseEvent ? event.clientX : rect.left + 12, y:event instanceof MouseEvent ? event.clientY : rect.bottom, items, owner:focus });
    };
    owner.addEventListener("contextmenu", open);
    owner.addEventListener("keydown", open);
    return () => { owner.removeEventListener("contextmenu", open); owner.removeEventListener("keydown", open); };
  }, [ref, selector, items]);
}

/** The open context menu: a point to hang from, and the ways it goes away that a menu hung from a
    control does not have — a resize or a scroll moves what was clicked out from under it. */
function PointMenu({ request, close }: { request: Request; close: () => void }) {
  // The point is kept 8 px inside the window: Base UI slides a menu along the edge only as far as
  // its anchor, and a click in the corner left the menu 2 px from the window's edge.
  const anchor = useMemo<VirtualAnchor>(() => {
    const x = Math.min(request.x, innerWidth - 8), y = Math.min(request.y, innerHeight - 8);
    return { getBoundingClientRect: () => ({ x, y, left:x, top:y, right:x, bottom:y, width:0, height:0 }), contextElement:request.owner };
  }, [request]);
  useEffect(() => {
    const dismiss = () => close();
    window.addEventListener("resize", dismiss);
    window.addEventListener("popstate", dismiss);
    // A scroll inside the menu is the menu's own; anywhere else it moves the page under it.
    const scrolled = (event: Event) => { if (!(event.target instanceof Element && event.target.closest(".menu"))) close(); };
    window.addEventListener("scroll", scrolled, true);
    return () => {
      window.removeEventListener("resize", dismiss);
      window.removeEventListener("popstate", dismiss);
      window.removeEventListener("scroll", scrolled, true);
      if (document.activeElement === document.body || document.activeElement?.closest(".context-menu")) request.owner.focus({ preventScroll:true });
    };
  }, [close, request.owner]);
  const items = useMemo(() => request.items.map((item) => (item !== "-" && "onSelect" in item && item.checked === undefined
    ? { ...item, onSelect:() => { request.owner.focus({ preventScroll:true }); return item.onSelect(); } }
    : item)), [request]);
  return <Menu anchor={anchor} open onClose={close} items={items} label={t("dlg.menu")} side="bottom" align="start" sheetBelow={0} className="context-menu" />;
}

export function ContextMenuHost() {
  const [request, setRequest] = useState<Request | null>(null);
  const close = useCallback(() => setRequest(null), []);
  useEffect(() => {
    const receive = (event: Event) => setRequest((event as RequestEvent).detail);
    // A long press with a finger also arrives as contextmenu. Taking it over in a text field hid the
    // phone's own selection handles and Paste, and our menu then closed at once (the press's release
    // and the keyboard resizing the viewport both dismiss it), so a reader whose keyboard has no
    // Paste key could neither paste nor select. A touch press is left to the platform entirely.
    let touch = false;
    const press = (event: PointerEvent) => { touch = event.pointerType === "touch" || event.pointerType === "pen"; };
    const fallback = (event: MouseEvent | KeyboardEvent) => {
      if (event.defaultPrevented) return;
      if (event.type === "contextmenu" && (touch || ("pointerType" in event && ((event as PointerEvent).pointerType === "touch" || (event as PointerEvent).pointerType === "pen")))) return;
      if (event instanceof KeyboardEvent && event.key !== "ContextMenu" && !(event.shiftKey && event.key === "F10")) return;
      const target = event.target as HTMLElement;
      const popup = target.closest<HTMLButtonElement>("button[aria-haspopup]");
      if (popup && !popup.disabled) {
        event.preventDefault();
        popup.click();
        return;
      }
      const field = target.closest<HTMLInputElement | HTMLTextAreaElement>("input, textarea");
      const editable = field ? null : target.closest<HTMLElement>("[contenteditable]:not([contenteditable='false'])");
      const owner = field ?? editable ?? target;
      const items: MenuItem[] = [];
      const selected = field ? field.value.slice(field.selectionStart ?? 0, field.selectionEnd ?? 0) : window.getSelection()?.toString() ?? "";
      const secret = field instanceof HTMLInputElement && field.type === "password";
      const report = (ok: boolean) => { if (!ok) toast(t("session.result.copy.failed")); };
      if (!secret && selected) items.push({ label:t("common.copy"), icon:"copy", onSelect:() => void copyText(selected).then(report) });
      if (field) {
        const start = field.selectionStart, end = field.selectionEnd;
        const editable = !field.readOnly && !field.disabled;
        const replace = (text: string) => {
          field.focus();
          if (start !== null && end !== null) field.setSelectionRange(start, end);
          // A browser edit emits input, keeping React, undo and the persisted draft in agreement.
          if (!document.execCommand("insertText", false, text)) toast(t("context.edit.failed"));
        };
        if (editable && !secret && selected) items.push({ label:t("context.cut"), icon:"cut", onSelect:() => void copyText(selected).then((ok) => { if (ok) replace(""); else report(false); }) });
        if (editable) items.push({ label:t("context.paste"), icon:"paste", onSelect:() => { if (navigator.clipboard) void navigator.clipboard.readText().then(replace).catch(() => toast(t("context.paste.failed"))); else toast(t("context.paste.failed")); } });
        items.push({ label:t("context.selectAll"), icon:"selectall", disabled:field.disabled, onSelect:() => { field.focus(); field.select(); } });
      } else if (editable) {
        // An editable element (the composer's field) has no value or selection range of its own: the
        // page's selection is it. The range is kept from the click, since the menu takes the focus.
        const held = window.getSelection()?.rangeCount ? window.getSelection()!.getRangeAt(0).cloneRange() : null;
        const restore = () => {
          editable.focus();
          if (!held) return;
          const now = window.getSelection();
          now?.removeAllRanges();
          now?.addRange(held);
        };
        const replace = (text: string) => {
          restore();
          if (!document.execCommand(text ? "insertText" : "delete", false, text)) toast(t("context.edit.failed"));
        };
        if (selected) items.push({ label:t("context.cut"), icon:"cut", onSelect:() => void copyText(selected).then((ok) => { if (ok) replace(""); else report(false); }) });
        items.push({ label:t("context.paste"), icon:"paste", onSelect:() => { if (navigator.clipboard) void navigator.clipboard.readText().then(replace).catch(() => toast(t("context.paste.failed"))); else toast(t("context.paste.failed")); } });
        items.push({ label:t("context.selectAll"), icon:"selectall", onSelect:() => {
          editable.focus();
          const range = document.createRange();
          range.selectNodeContents(editable);
          window.getSelection()?.removeAllRanges();
          window.getSelection()?.addRange(range);
        } });
      }
      const link = target.closest<HTMLAnchorElement>("a[href]");
      if (link) items.push(
        { label:t("common.open"), icon:"external", onSelect:() => link.click() },
        { label:t("context.copyLink"), icon:"link", onSelect:() => void copyText(link.href).then(report) },
      );
      // Nothing here belongs to what was clicked: no menu of ours. It used to offer "Search and go"
      // on every such click, an entry about nothing under the pointer; the browser's own menu is
      // left to answer instead.
      if (!items.length) return;
      event.preventDefault();
      const rect = owner.getBoundingClientRect();
      setRequest({ x:event instanceof MouseEvent ? event.clientX : rect.left, y:event instanceof MouseEvent ? event.clientY : rect.bottom, items, owner });
    };
    document.addEventListener(EVENT, receive);
    document.addEventListener("pointerdown", press, true);
    document.addEventListener("contextmenu", fallback);
    document.addEventListener("keydown", fallback);
    return () => { document.removeEventListener(EVENT, receive); document.removeEventListener("pointerdown", press, true); document.removeEventListener("contextmenu", fallback); document.removeEventListener("keydown", fallback); };
  }, []);
  return request && <PointMenu request={request} close={close} />;
}
