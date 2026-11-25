// The phone's drawer: a left sheet over a scrim that holds the sidebar (drawer.tsx draws what is in it),
// the one store that says whether it is open, and the hamburger every screen's bar opens it with.
// What is phone-only here is behaviour: the swipe that closes it, the history entry Back takes, the
// focus it holds and gives back.

import { type CSSProperties, type ReactNode, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { inLayer, pushLayer } from "../../router";
import { t } from "../../i18n";
import { IconButton } from "./controls";
import { trapDialogTab, useLayer } from "./layers";

// ── the drawer's open state ─────────────────────────────────────────────────────────────────────
// One drawer for the whole app, opened from whichever screen's hamburger was pressed: a module store
// rather than a context, so a screen deep in a lazy chunk opens it without a provider in between.

let drawerOpen = false;
let drawerOpener: HTMLElement | null = null;
const drawerListeners = new Set<() => void>();

export function openDrawer(): void {
  if (drawerOpen) return;
  drawerOpener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  drawerOpen = true;
  drawerListeners.forEach((l) => l());
}

export function closeDrawer(): void {
  if (!drawerOpen) return;
  drawerOpen = false;
  drawerListeners.forEach((l) => l());
}

export function useDrawerOpen(): boolean {
  return useSyncExternalStore(
    (l) => { drawerListeners.add(l); return () => { drawerListeners.delete(l); }; },
    () => drawerOpen,
  );
}

/** The hamburger. `pip` is the amber mark of something waiting for the operator somewhere in the drawer. */
export function MenuButton({ pip }: { pip?: boolean }) {
  const open = useDrawerOpen();
  return <IconButton icon="menu" label={t("nav.menu")} onClick={openDrawer} pip={pip} className="ph-ib ph-menu" expanded={open} popup="dialog" />;
}

// ── the drawer ──────────────────────────────────────────────────────────────────────────────────

/**
 * A sheet from the left over a scrim, holding the sidebar (drawer.tsx). It takes a history entry
 * while open, so Android's back gesture and the browser's Back close it instead of leaving the page,
 * and a move from inside it takes that entry's place (router.ts); Escape, a tap on the scrim and a
 * swipe to the left close it too. Focus moves into it on open, Tab stays inside, and focus returns to the
 * control that opened it. Its contents are drawn only while it is open (and through the slide that
 * closes it): mounted but inert, its rows of recents duplicated every row of the page under it for
 * anything that looked a chat up by its id.
 */
export function Drawer({ open, onClose, label, children, className = "" }: { open: boolean; onClose: () => void; label: string; children: ReactNode; className?: string }) {
  const panel = useRef<HTMLElement>(null);
  const [drag, setDrag] = useState(0);
  const [mounted, setMounted] = useState(open);
  useEffect(() => {
    if (open) { setMounted(true); return; }
    const timer = window.setTimeout(() => setMounted(false), 260);
    return () => window.clearTimeout(timer);
  }, [open]);
  const start = useRef<{ x: number; y: number; id: number; horizontal: boolean | null } | null>(null);

  // The history entry: pushed on open, taken back on a close that was not itself a Back.
  useEffect(() => {
    if (!open) return;
    pushLayer();
    const onPop = () => { if (!inLayer()) onClose(); };
    window.addEventListener("popstate", onPop);
    return () => {
      window.removeEventListener("popstate", onPop);
      if (inLayer()) window.history.back();
    };
    // onClose is a fresh function on every render of the shell; the entry belongs to the open state.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useEffect(() => {
    if (!open) return;
    panel.current?.focus();
    return () => {
      const back = drawerOpener;
      drawerOpener = null;
      if (back && document.contains(back)) back.focus();
    };
  }, [open]);

  return (
    <div className={`ph-drawer-root ${open ? "open" : ""} ${className}`}>
      <div className="ph-scrim" onClick={onClose} aria-hidden />
      <DrawerEscape open={open} onClose={onClose} />
      <aside
        ref={panel}
        className="ph-drawer"
        role="dialog"
        aria-modal="true"
        aria-label={label}
        tabIndex={-1}
        inert={!open}
        aria-hidden={!open}
        onKeyDown={trapDialogTab}
        style={drag < 0 ? ({ transform: `translateX(${drag}px)`, transition: "none" } as CSSProperties) : undefined}
        onPointerDown={(e) => { if (e.pointerType !== "mouse") start.current = { x: e.clientX, y: e.clientY, id: e.pointerId, horizontal: null }; }}
        onPointerMove={(e) => {
          const s = start.current;
          if (!s || s.id !== e.pointerId) return;
          const dx = e.clientX - s.x, dy = e.clientY - s.y;
          if (s.horizontal === null && Math.abs(dx) + Math.abs(dy) > 10) s.horizontal = Math.abs(dx) > Math.abs(dy);
          if (s.horizontal) setDrag(Math.min(0, dx));
        }}
        onPointerUp={() => {
          const closing = drag < -72;
          start.current = null;
          setDrag(0);
          if (closing) onClose();
        }}
        onPointerCancel={() => { start.current = null; setDrag(0); }}
      >
        {(open || mounted) && children}
      </aside>
    </div>
  );
}

/** Escape closes the drawer only while it is the top layer, the way it closes a sheet. */
function DrawerEscape({ open, onClose }: { open: boolean; onClose: () => void }) {
  if (!open) return null;
  return <EscapeLayer onEscape={onClose} />;
}

function EscapeLayer({ onEscape }: { onEscape: () => void }) {
  useLayer(onEscape);
  return null;
}
