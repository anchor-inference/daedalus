import { useCallback, useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react";
import { createPortal } from "react-dom";
import { Icon } from "../icons";
import { t } from "../i18n";
import { copyText } from "./components";
import { toast, useLayer, type MenuItem } from "./dialogs";

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
      if ((event.target as HTMLElement).closest("input, textarea, [contenteditable='true']")) return;
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

function Menu({ request, close }: { request: Request; close: () => void }) {
  const box = useRef<HTMLDivElement>(null);
  const [point, setPoint] = useState({ left:request.x, top:request.y });
  useLayer(close);
  useLayoutEffect(() => {
    const place = () => {
      const rect = box.current!.getBoundingClientRect();
      setPoint({ left:Math.max(8, Math.min(request.x, innerWidth - rect.width - 8)), top:Math.max(8, Math.min(request.y, innerHeight - rect.height - 8)) });
    };
    place();
    box.current?.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus();
    const observer = new ResizeObserver(place);
    observer.observe(box.current!);
    return () => observer.disconnect();
  }, [request]);
  useEffect(() => {
    const outside = (event: Event) => { if (!box.current?.contains(event.target as Node)) close(); };
    const dismiss = () => close();
    document.addEventListener("pointerdown", outside);
    window.addEventListener("resize", dismiss);
    window.addEventListener("scroll", dismiss, true);
    window.addEventListener("popstate", dismiss);
    return () => {
      document.removeEventListener("pointerdown", outside);
      window.removeEventListener("resize", dismiss);
      window.removeEventListener("scroll", dismiss, true);
      window.removeEventListener("popstate", dismiss);
      if (document.activeElement === document.body || box.current?.contains(document.activeElement)) request.owner.focus({ preventScroll:true });
    };
  }, [close, request.owner]);
  return createPortal(<div ref={box} className="menu context-menu" role="menu" aria-label={t("dlg.menu")} style={{ position:"fixed", ...point }} onContextMenu={(event) => { event.preventDefault(); event.stopPropagation(); }} onKeyDown={(event) => {
    const buttons = Array.from(box.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") ?? []);
    const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
    if (event.key === "Tab") { event.preventDefault(); close(); request.owner.focus(); return; }
    const next = event.key === "ArrowDown" ? (index + 1) % buttons.length : event.key === "ArrowUp" ? (index - 1 + buttons.length) % buttons.length : event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : null;
    if (next !== null) { event.preventDefault(); buttons[next]?.focus(); }
  }}>{request.items.map((item, index) => item === "-" ? <div key={index} className="menu-sep" /> : <button key={index} role={item.checked === undefined ? "menuitem" : "menuitemcheckbox"} aria-checked={item.checked} disabled={item.disabled} className={item.danger ? "danger" : item.warn ? "warn" : ""} title={item.hint} onClick={() => { close(); request.owner.focus({ preventScroll:true }); item.onSelect(); }}>{item.icon && <Icon name={item.icon} size={16} />}{item.label}</button>)}</div>, document.body);
}

export function ContextMenuHost() {
  const [request, setRequest] = useState<Request | null>(null);
  const close = useCallback(() => setRequest(null), []);
  useEffect(() => {
    const receive = (event: Event) => setRequest((event as RequestEvent).detail);
    const fallback = (event: MouseEvent | KeyboardEvent) => {
      if (event.defaultPrevented) return;
      if (event instanceof KeyboardEvent && event.key !== "ContextMenu" && !(event.shiftKey && event.key === "F10")) return;
      const target = event.target as HTMLElement;
      const popup = target.closest<HTMLButtonElement>("button[aria-haspopup]");
      if (popup && !popup.disabled) {
        event.preventDefault();
        popup.click();
        return;
      }
      const field = target.closest<HTMLInputElement | HTMLTextAreaElement>("input, textarea");
      const owner = field ?? target;
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
        if (editable && !secret && selected) items.push({ label:t("context.cut"), onSelect:() => void copyText(selected).then((ok) => { if (ok) replace(""); else report(false); }) });
        if (editable) items.push({ label:t("context.paste"), onSelect:() => { if (navigator.clipboard) void navigator.clipboard.readText().then(replace).catch(() => toast(t("context.paste.failed"))); else toast(t("context.paste.failed")); } });
        items.push({ label:t("context.selectAll"), disabled:field.disabled, onSelect:() => { field.focus(); field.select(); } });
      }
      const link = target.closest<HTMLAnchorElement>("a[href]");
      if (link) items.push(
        { label:t("common.open"), onSelect:() => link.click() },
        { label:t("context.copyLink"), icon:"link", onSelect:() => void copyText(link.href).then(report) },
      );
      if (!items.length) items.push({ label:t("shell.search.label"), icon:"search", onSelect:() => document.dispatchEvent(new KeyboardEvent("keydown", { key:"k", ctrlKey:true, bubbles:true })) });
      event.preventDefault();
      const rect = owner.getBoundingClientRect();
      setRequest({ x:event instanceof MouseEvent ? event.clientX : rect.left, y:event instanceof MouseEvent ? event.clientY : rect.bottom, items, owner });
    };
    document.addEventListener(EVENT, receive);
    document.addEventListener("contextmenu", fallback);
    document.addEventListener("keydown", fallback);
    return () => { document.removeEventListener(EVENT, receive); document.removeEventListener("contextmenu", fallback); document.removeEventListener("keydown", fallback); };
  }, []);
  return request && <Menu request={request} close={close} />;
}
