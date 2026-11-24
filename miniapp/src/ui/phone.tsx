// The phone's building blocks. Every screen below 1024 px is drawn from these, so a screen states
// what it shows and leaves the sizes, the gestures and the keyboard to this file; ui/phone.css holds
// their looks, all of it inside the phone media query, which is why a desktop never changes when they
// do. The sizes come from the phone tokens in ui/tokens.css (--top-h, --list-row-h, --chip-h…).
//
//   TopBar          the page's bar: hamburger or back, a title (a button when it holds more), ≤ 2 glyphs
//   MenuButton      the hamburger that opens the app's drawer (openDrawer), from any screen
//   Drawer          a left sheet with a scrim: Escape, Back, a swipe and the scrim close it; focus stays in
//   SectionHeader   a list's group label: 15/600 grey, a count, an optional action at the right
//   Chip, ChipBar   a 32 px filter pill (44 px to the finger) and the scrolling row that holds them
//   EmptyState      a title, the hint and one action, centred, for empty, error and filtered-empty
//   RowSkeleton     a list's placeholder rows at the real row height while it loads
//   Banner          a 32 px strip under the top bar (offline) or a tinted note with an action
//   Group, GroupRow a grouped card of 56 px settings rows with 2 px gaps and 20/6 px corners
//   NewChatPill     the one accent creation pill: in the drawer's foot, or floating over a list
//
// The pieces both sizes share live in ui/primitives: the sheet, the menu and its rows, the list row,
// the icon button and the segmented choice. A phone screen dresses an icon button and a segmented
// choice in its own sizes with the `ph-ib` and `ph-seg` classes until its screen moves onto the
// shared sizes.

import { type CSSProperties, type ReactNode, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { Icon, type IconName } from "../icons";
import { inLayer, navigate, pushLayer } from "../router";
import { t } from "../i18n";
import { IconButton } from "./primitives/controls";
import { trapDialogTab, useLayer } from "./primitives/layers";

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

/** Go somewhere from inside the drawer: the drawer's own history entry is replaced, so Back from the
 *  destination returns to the page under the drawer rather than reopening it. */
export function navigateFromDrawer(path: string): void {
  navigate(path, { replace: inLayer() });
  closeDrawer();
}

// ── the top bar ─────────────────────────────────────────────────────────────────────────────────

/** The hamburger. `pip` is the amber mark of something waiting for the operator somewhere in the drawer. */
export function MenuButton({ pip }: { pip?: boolean }) {
  const open = useDrawerOpen();
  return <IconButton icon="menu" label={t("nav.menu")} onClick={openDrawer} pip={pip} className="ph-ib ph-menu" expanded={open} popup="dialog" />;
}

export type TopBarProps = {
  title: ReactNode;
  /** The second line: the model, the live state, a count. A detail page's title with one sits left;
   *  a top-level page that asks for `center` keeps both lines centred (the Inbox's "4 unread"). */
  sub?: ReactNode;
  /** Centred, as a top-level page's title is. */
  center?: boolean;
  /** A pushed page: the back arrow instead of the hamburger. */
  back?: () => void;
  /** The way out drawn as another glyph and name (a selection's ✕ "Done selecting"). */
  backIcon?: IconName;
  backLabel?: string;
  /** No hamburger and no back (a full-screen flow with its own way out). */
  bare?: boolean;
  pip?: boolean;
  /** The title is a button when it holds more than it shows: the full title, the model, the live state. */
  onTitle?: () => void;
  titleLabel?: string;
  /** At most two IconButtons; everything else belongs in a ⋮ sheet. */
  actions?: ReactNode;
  className?: string;
};

export function TopBar({ title, sub, center, back, backIcon, backLabel, bare, pip, onTitle, titleLabel, actions, className = "" }: TopBarProps) {
  const words = (
    <>
      <span className="ph-top-t">{title}</span>
      {sub && <span className="ph-top-sub">{sub}</span>}
    </>
  );
  return (
    <header className={`ph-top ${className}`}>
      {back ? <IconButton icon={backIcon ?? "back"} label={backLabel ?? t("shell.back")} onClick={back} className="ph-ib" /> : !bare && <MenuButton pip={pip} />}
      <div className={`ph-top-title ${center ? "center" : ""}`}>
        {onTitle ? <button type="button" className="ph-top-tb" onClick={onTitle} aria-label={titleLabel}>{words}</button> : words}
      </div>
      {actions ? <div className="ph-top-actions">{actions}</div> : center ? <span className="ph-top-spacer" aria-hidden /> : null}
    </header>
  );
}

// ── the drawer ──────────────────────────────────────────────────────────────────────────────────

/**
 * A sheet from the left over a scrim. It takes a history entry while open, so Android's back gesture
 * and the browser's Back close it instead of leaving the page; Escape, a tap on the scrim and a swipe
 * to the left close it too. Focus moves into it on open, Tab stays inside, and focus returns to the
 * control that opened it. Its contents are drawn only while it is open (and through the slide that
 * closes it): mounted but inert, its rows of recents duplicated every row of the page under it for
 * anything that looked a chat up by its id.
 */
export function Drawer({ open, onClose, label, children, footer, className = "" }: { open: boolean; onClose: () => void; label: string; children: ReactNode; footer?: ReactNode; className?: string }) {
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
        <div className="ph-drawer-body">{(open || mounted) && children}</div>
        {footer && (open || mounted) && <div className="ph-drawer-foot">{footer}</div>}
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

export function SectionHeader({ children, count, action, tone }: { children: ReactNode; count?: ReactNode; action?: ReactNode; tone?: "warn" | "bad" }) {
  return (
    <div className={`ph-sec ${tone ?? ""}`}>
      <span className="ph-sec-t">{children}</span>
      {count !== undefined && count !== null && <span className="ph-sec-n">{count}</span>}
      {action && <span className="ph-sec-act">{action}</span>}
    </div>
  );
}

// ── choices ─────────────────────────────────────────────────────────────────────────────────────

export function Chip({ on, tone, count, onClick, children, label }: { on?: boolean; tone?: "warn"; count?: ReactNode; onClick: () => void; children: ReactNode; label?: string }) {
  return (
    <button type="button" className={`ph-chip ${on ? "on" : ""} ${tone ?? ""}`} aria-pressed={!!on} onClick={onClick} aria-label={label}>
      <span className="ph-chip-l">{children}</span>
      {count !== undefined && count !== null && <span className="ph-chip-n">{count}</span>}
    </button>
  );
}

export function ChipBar({ children, label }: { children: ReactNode; label: string }) {
  return <div className="ph-chips" role="group" aria-label={label}>{children}</div>;
}

// ── states ──────────────────────────────────────────────────────────────────────────────────────

export function EmptyState({ icon, tone, title, body, action, className = "" }: { icon: IconName; tone?: "bad" | "ok"; title: ReactNode; body?: ReactNode; action?: ReactNode; className?: string }) {
  return (
    <div className={`ph-empty ${className}`} role={tone === "bad" ? "alert" : undefined}>
      <span className={`ph-empty-ico ${tone ?? ""}`}><Icon name={icon} size={22} /></span>
      <span className="ph-empty-t">{title}</span>
      {body && <span className="ph-empty-m">{body}</span>}
      {action}
    </div>
  );
}

/** A list's rows while it loads, at the rows' real height, so nothing jumps when they arrive. */
export function RowSkeleton({ rows = 6, lead = "round" }: { rows?: number; lead?: "round" | "square" | "none" }) {
  const widths = [[62, 38], [48, 44], [70, 30], [55, 35], [66, 42], [40, 33], [58, 40], [52, 36]];
  return (
    <div aria-busy="true" aria-label={t("common.loading")}>
      {widths.slice(0, rows).map(([a, b], i) => (
        <div key={i} className="ph-skrow">
          {lead !== "none" && <span className="ph-sk" style={{ width: 36, height: 36, borderRadius: lead === "round" ? "50%" : 10 }} />}
          <span className="grow"><span className="ph-sk" style={{ height: 14, width: `${a}%` }} /><span className="ph-sk" style={{ height: 11, width: `${b}%`, marginTop: 8 }} /></span>
        </div>
      ))}
    </div>
  );
}

/** `strip`: the 32 px line under the top bar (offline, reconnecting). Otherwise a tinted note with an
 *  icon, the words, a second line and one action under them. */
export function Banner({ tone, icon, children, sub, action, strip, className = "" }: { tone?: "bad" | "warn" | "info"; icon?: IconName; children: ReactNode; sub?: ReactNode; action?: ReactNode; strip?: boolean; className?: string }) {
  if (strip) {
    return <div className={`ph-strip ${tone ?? ""} ${className}`} role="status">{icon && <Icon name={icon} size={14} />}<span>{children}</span></div>;
  }
  return (
    <div className={`ph-note ${tone ?? ""} ${className}`} role={tone === "bad" ? "alert" : "status"}>
      {icon && <Icon name={icon} size={18} />}
      <div className="ph-note-main">
        <div>{children}</div>
        {sub && <div className="ph-note-m">{sub}</div>}
        {action && <div className="ph-note-act">{action}</div>}
      </div>
    </div>
  );
}

// ── grouped cards ───────────────────────────────────────────────────────────────────────────────

export function Group({ label, children }: { label?: ReactNode; children: ReactNode }) {
  return (
    <>
      {label && <div className="ph-gl">{label}</div>}
      <div className="ph-group">{children}</div>
    </>
  );
}

export function GroupRow({ icon, title, sub, value, href, onClick, danger, chevron, children }: { icon?: IconName; title: ReactNode; sub?: ReactNode; value?: ReactNode; href?: string; onClick?: () => void; danger?: boolean; chevron?: boolean; children?: ReactNode }) {
  const body = (
    <>
      {icon && <Icon name={icon} size={20} />}
      <span className="ph-srow-main">
        <span className="ph-srow-t">{title}</span>
        {sub && <span className="ph-srow-v">{sub}</span>}
      </span>
      {value !== undefined && <span className="ph-srow-val">{value}</span>}
      {children}
      {chevron && <span className="ph-srow-chev"><Icon name="forward" size={18} /></span>}
    </>
  );
  const cls = `ph-srow ${sub ? "two" : ""} ${danger ? "danger" : ""}`;
  if (href) return <a className={cls} href={href} onClick={(e) => { if (e.metaKey || e.ctrlKey || e.shiftKey) return; e.preventDefault(); onClick ? onClick() : navigate(href); }}>{body}</a>;
  if (onClick) return <button type="button" className={cls} onClick={onClick}>{body}</button>;
  return <div className={cls}>{body}</div>;
}

export function NewChatPill({ label, icon = "compose", onClick, floating }: { label: string; icon?: IconName; onClick: () => void; floating?: boolean }) {
  return (
    <button type="button" className={`ph-newchat ${floating ? "fab" : ""}`} onClick={onClick}>
      <Icon name={icon} size={18} />
      <span>{label}</span>
    </button>
  );
}
