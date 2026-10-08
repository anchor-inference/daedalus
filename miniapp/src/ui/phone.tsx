// The phone's building blocks. Every screen below 1024 px is drawn from these, so a screen states
// what it shows and leaves the sizes, the gestures and the keyboard to this file; ui/phone.css holds
// their looks, all of it inside the phone media query, which is why a desktop never changes when they
// do. The sizes come from the phone tokens in ui/tokens.css (--top-h, --list-row-h, --chip-h…).
//
//   TopBar          the page's bar: hamburger or back, a title (a button when it holds more), ≤ 2 glyphs
//   IconButton      a bare 22 px glyph in a 44 px target, with an optional badge or pip
//   MenuButton      the hamburger that opens the app's drawer (openDrawer), from any screen
//   Drawer          a left sheet with a scrim: Escape, Back, a swipe and the scrim close it; focus stays in
//   BottomSheet     the app's Sheet with a preview card and a sticky footer; handle and 20 px corners
//   SheetRow        a 52 px row of a sheet or menu: icon, label, hint, value, check, danger
//   ActionSheet     the long-press sheet: a preview of the row and its commands, destructive last
//   ListRow         a 60 px borderless row; long-press, right click and its ⋮ open its ActionSheet,
//                   a horizontal swipe reveals up to three quick actions
//   SectionHeader   a list's group label: 15/600 grey, a count, an optional action at the right
//   SegmentedControl a pill of two to four choices; each choice may carry a badge
//   Chip, ChipBar   a 32 px filter pill (44 px to the finger) and the scrolling row that holds them
//   EmptyState      a title, the hint and one action, centred, for empty, error and filtered-empty
//   RowSkeleton     a list's placeholder rows at the real row height while it loads
//   Banner          a 32 px strip under the top bar (offline) or a tinted note with an action
//   Group, GroupRow a grouped card of 56 px settings rows with 2 px gaps and 20/6 px corners
//   NewChatPill     the one accent creation pill: in the drawer's foot, or floating over a list
//
// Long-press, the ⋮ and the context menu all open the same sheet, so an action is never reachable by
// a gesture alone: a reader who never long-presses still finds every command behind the ⋮.

import { type CSSProperties, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent, type ReactNode, useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { Icon, type IconName } from "../icons";
import { inLayer, navigate, pushLayer } from "../router";
import { t } from "../i18n";
import { Sheet, trapDialogTab, useLayer, type MenuItem } from "./dialogs";

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

export function IconButton({ icon, label, onClick, href, badge, pip, className = "", disabled, expanded, popup }: { icon: IconName; label: string; onClick?: () => void; href?: string; badge?: number | string | null; pip?: boolean; className?: string; disabled?: boolean; expanded?: boolean; popup?: "menu" | "dialog" }) {
  const inner = (
    <>
      <Icon name={icon} size={22} />
      {badge !== undefined && badge !== null && badge !== 0 && <span className="ph-badge" aria-hidden>{typeof badge === "number" && badge > 99 ? "99+" : badge}</span>}
      {pip && <span className="ph-pip" aria-hidden />}
    </>
  );
  if (href) {
    return (
      <a className={`ph-ib ${className}`} href={href} aria-label={label} title={label} onClick={(e) => { if (e.metaKey || e.ctrlKey || e.shiftKey) return; e.preventDefault(); onClick ? onClick() : navigate(href); }}>
        {inner}
      </a>
    );
  }
  return (
    <button type="button" className={`ph-ib ${className}`} aria-label={label} title={label} onClick={onClick} disabled={disabled} aria-expanded={expanded} aria-haspopup={popup}>
      {inner}
    </button>
  );
}

/** The hamburger. `pip` is the amber mark of something waiting for the operator somewhere in the drawer. */
export function MenuButton({ pip }: { pip?: boolean }) {
  const open = useDrawerOpen();
  return <IconButton icon="menu" label={t("nav.menu")} onClick={openDrawer} pip={pip} className="ph-menu" expanded={open} popup="dialog" />;
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
      {back ? <IconButton icon={backIcon ?? "back"} label={backLabel ?? t("shell.back")} onClick={back} /> : !bare && <MenuButton pip={pip} />}
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

// ── sheets ──────────────────────────────────────────────────────────────────────────────────────

export function SheetRow({ icon, label, hint, value, danger, checked, chevron, disabled, onClick, children, data }: { icon?: IconName; label: ReactNode; hint?: ReactNode; value?: ReactNode; danger?: boolean; checked?: boolean; chevron?: boolean; disabled?: boolean; onClick?: () => void; children?: ReactNode; data?: Record<string, string> }) {
  const attrs = Object.fromEntries(Object.entries(data ?? {}).map(([k, v]) => [`data-${k}`, v]));
  const body = (
    <>
      {icon && <Icon name={icon} size={22} />}
      <span className="ph-mrow-main">
        <span className="ph-mrow-t">{label}</span>
        {hint && <span className="ph-mrow-m">{hint}</span>}
      </span>
      {value !== undefined && <span className="ph-mrow-val">{value}</span>}
      {children}
      {checked && <span className="ph-mrow-chk"><Icon name="check" size={18} /></span>}
      {chevron && <span className="ph-mrow-chev"><Icon name="forward" size={18} /></span>}
    </>
  );
  if (!onClick) return <div className={`ph-mrow ${danger ? "danger" : ""}`} {...attrs}>{body}</div>;
  return <button type="button" {...attrs} className={`ph-mrow ${danger ? "danger" : ""} ${checked ? "on" : ""}`} onClick={onClick} disabled={disabled} role={checked === undefined ? undefined : "menuitemradio"} aria-checked={checked === undefined ? undefined : checked}>{body}</button>;
}

export type Preview = { title: ReactNode; meta?: ReactNode };

/** The app's Sheet in the phone's dress: a preview card above the body and a sticky footer when the
 *  sheet ends in a decision. The handle, the corners and the title size come from ui/phone.css. */
export function BottomSheet({ title, onClose, preview, footer, children, full, className = "", label }: { title?: ReactNode; onClose: () => void; preview?: Preview; footer?: ReactNode; children: ReactNode; full?: boolean; className?: string; label?: string }) {
  return (
    <Sheet title={title} ariaLabel={label ?? (typeof preview?.title === "string" ? preview.title : undefined)} onClose={onClose} size={full ? "full" : undefined} className={`ph-sheet ${title ? "" : "untitled"} ${className}`}>
      {preview && (
        <div className="ph-preview">
          <div className="ph-preview-t">{preview.title}</div>
          {preview.meta && <div className="ph-preview-m">{preview.meta}</div>}
        </div>
      )}
      {children}
      {footer && <div className="sheet-foot ph-sheet-foot">{footer}</div>}
    </Sheet>
  );
}

/** A row's commands as a sheet: the destructive ones are moved last, after a divider, whatever order
 *  the menu that also lists them uses. */
export function ActionSheet({ items, preview, title, onClose }: { items: MenuItem[]; preview?: Preview; title?: ReactNode; onClose: () => void }) {
  const real = items.filter((item): item is Exclude<MenuItem, "-"> => item !== "-");
  const safe = real.filter((item) => !item.danger);
  const danger = real.filter((item) => item.danger);
  const pick = (item: Exclude<MenuItem, "-">) => {
    if (item.checked === undefined) onClose();
    item.onSelect();
  };
  return (
    <BottomSheet title={title} preview={preview} onClose={onClose} className="ph-actions" label={typeof preview?.title === "string" ? preview.title : t("dlg.menu")}>
      <div role="menu">
        {safe.map((item, i) => <SheetRow key={i} icon={item.icon} label={item.label} hint={item.hint} disabled={item.disabled} checked={item.checked} onClick={() => pick(item)} />)}
        {danger.length > 0 && safe.length > 0 && <div className="ph-msep" role="separator" />}
        {danger.map((item, i) => <SheetRow key={`d${i}`} icon={item.icon} label={item.label} hint={item.hint} disabled={item.disabled} danger onClick={() => pick(item)} />)}
      </div>
    </BottomSheet>
  );
}

// ── list rows ───────────────────────────────────────────────────────────────────────────────────

export type SwipeAction = { label: string; icon: IconName; tone?: "neutral" | "info" | "danger" | "accent"; onSelect: () => void };

const LONG_PRESS_MS = 450;
const SWIPE_W = 76;

/**
 * A long press without a library: a finger held still for 450 ms. Android also sends `contextmenu`
 * for the same press (and a mouse sends it for a right click), so both roads lead to one handler and
 * the click that follows the release is swallowed rather than opening the row under the sheet.
 */
export function useLongPress(onLong: () => void) {
  const timer = useRef<number | null>(null);
  const fired = useRef(false);
  // When the held finger last opened the sheet: the contextmenu Android sends for that same press
  // comes within a moment and is the same request. A flag alone stayed set after a right click (a
  // mouse's secondary press never reset it), and the next right click on that row did nothing.
  const firedAt = useRef(Number.NEGATIVE_INFINITY);
  const origin = useRef<{ x: number; y: number } | null>(null);
  const cancel = useCallback(() => {
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = null;
  }, []);
  useEffect(() => cancel, [cancel]);
  return {
    fired,
    cancel,
    handlers: {
      onPointerDown: (e: ReactPointerEvent) => {
        if (e.pointerType === "mouse" && e.button !== 0) return;
        fired.current = false;
        origin.current = { x: e.clientX, y: e.clientY };
        cancel();
        if (e.pointerType === "mouse") return;
        timer.current = window.setTimeout(() => { fired.current = true; firedAt.current = performance.now(); timer.current = null; onLong(); }, LONG_PRESS_MS);
      },
      onPointerMove: (e: ReactPointerEvent) => {
        const o = origin.current;
        if (o && Math.abs(e.clientX - o.x) + Math.abs(e.clientY - o.y) > 10) cancel();
      },
      onPointerUp: cancel,
      onPointerCancel: cancel,
      onContextMenu: (e: ReactMouseEvent) => {
        e.preventDefault();
        e.stopPropagation();
        cancel();
        if (performance.now() - firedAt.current < 1000) return;
        fired.current = true;
        onLong();
      },
    },
  };
}

export type ListRowProps = {
  title: ReactNode;
  meta?: ReactNode;
  lead?: ReactNode;
  trail?: ReactNode;
  onOpen: () => void;
  /** The row's commands: long-press, right click and the ⋮ all open them as an ActionSheet. */
  actions?: MenuItem[];
  preview?: Preview;
  /** Revealed by a swipe to the left (up to three), or `swipeStart` by a swipe to the right (one). */
  swipe?: SwipeAction[];
  swipeStart?: SwipeAction;
  /** One line only (52 px): a title without a meta line. */
  one?: boolean;
  /** More under the meta line: a command's text, answers. Presses on it do not open the row. */
  body?: ReactNode;
  /** The ⋮ beside the row; off where the row's commands are reachable elsewhere on the page (a
   *  selection mode, the detail sheet), so a dense list keeps its width for the words. */
  more?: boolean;
  unread?: boolean;
  current?: boolean;
  /** The accessible name of the ⋮; the title is used when it is a string. */
  label?: string;
  className?: string;
  data?: Record<string, string>;
};

export function ListRow({ title, meta, lead, trail, onOpen, actions, preview, swipe, swipeStart, one, body, more = true, unread, current, label, className = "", data }: ListRowProps) {
  const [sheet, setSheet] = useState(false);
  const [offset, setOffset] = useState(0);
  const [dragging, setDragging] = useState(false);
  const hasActions = !!actions && actions.some((a) => a !== "-");
  const press = useLongPress(() => { if (hasActions) setSheet(true); });
  const drag = useRef<{ x: number; y: number; base: number; horizontal: boolean | null } | null>(null);
  const leftMax = swipe?.length ? -SWIPE_W * Math.min(3, swipe.length) : 0;
  const rightMax = swipeStart ? SWIPE_W : 0;
  const name = label ?? (typeof title === "string" ? title : t("dlg.menu"));
  const attrs = Object.fromEntries(Object.entries(data ?? {}).map(([k, v]) => [`data-${k}`, v]));

  const down = (e: ReactPointerEvent<HTMLDivElement>) => {
    press.handlers.onPointerDown(e);
    if ((leftMax || rightMax) && e.pointerType !== "mouse") drag.current = { x: e.clientX, y: e.clientY, base: offset, horizontal: null };
  };
  const move = (e: ReactPointerEvent<HTMLDivElement>) => {
    press.handlers.onPointerMove(e);
    const d = drag.current;
    if (!d) return;
    const dx = e.clientX - d.x, dy = e.clientY - d.y;
    if (d.horizontal === null && Math.abs(dx) + Math.abs(dy) > 10) d.horizontal = Math.abs(dx) > Math.abs(dy);
    if (!d.horizontal) return;
    setDragging(true);
    setOffset(Math.max(leftMax, Math.min(rightMax, d.base + dx)));
  };
  const up = () => {
    press.handlers.onPointerUp();
    const d = drag.current;
    drag.current = null;
    if (!d?.horizontal) return;
    setDragging(false);
    // Past half of the actions' width the row stays open on them; short of it, it springs back.
    setOffset((o) => (o < leftMax / 2 ? leftMax : o > rightMax / 2 && rightMax ? rightMax : 0));
    press.fired.current = true;
  };
  const click = () => {
    if (press.fired.current) { press.fired.current = false; return; }
    if (offset !== 0) { setOffset(0); return; }
    onOpen();
  };
  const run = (action: SwipeAction) => { setOffset(0); action.onSelect(); };

  return (
    <div className={`ph-row-wrap ${offset !== 0 ? "swiped" : ""}`} {...attrs}>
      {swipeStart && offset > 0 && (
        <div className="ph-swipe start">
          <button type="button" className={`ph-swipe-act ${swipeStart.tone ?? "accent"}`} onClick={() => run(swipeStart)}><Icon name={swipeStart.icon} size={18} />{swipeStart.label}</button>
        </div>
      )}
      {swipe && offset < 0 && (
        <div className="ph-swipe end">
          {swipe.slice(0, 3).map((action) => (
            <button key={action.label} type="button" className={`ph-swipe-act ${action.tone ?? "neutral"}`} onClick={() => run(action)}><Icon name={action.icon} size={18} />{action.label}</button>
          ))}
        </div>
      )}
      <div
        className={`ph-row ${one ? "one" : ""} ${unread ? "unread" : ""} ${current ? "current" : ""} ${sheet ? "pressed" : ""} ${className}`}
        role="link"
        tabIndex={0}
        aria-current={current ? "page" : undefined}
        style={offset !== 0 || dragging ? ({ transform: `translateX(${offset}px)`, transition: dragging ? "none" : undefined } as CSSProperties) : undefined}
        onClick={click}
        onKeyDown={(e) => {
          if (e.target !== e.currentTarget) return;
          if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onOpen(); }
          else if (hasActions && (e.key === "ContextMenu" || (e.shiftKey && e.key === "F10"))) { e.preventDefault(); setSheet(true); }
        }}
        onPointerDown={down}
        onPointerMove={move}
        onPointerUp={up}
        onPointerCancel={() => { press.handlers.onPointerCancel(); drag.current = null; setDragging(false); setOffset(0); }}
        onContextMenu={hasActions ? press.handlers.onContextMenu : undefined}
      >
        {lead && <span className="ph-row-lead">{lead}</span>}
        <span className="ph-row-main">
          <span className="ph-row-t">{title}</span>
          {meta && !one && <span className="ph-row-m">{meta}</span>}
          {body && <span className="ph-row-b" onClick={stopOnControl} onPointerDown={stopOnControl}>{body}</span>}
        </span>
        {trail !== undefined && <span className="ph-row-trail">{trail}</span>}
        {hasActions && more && (
          <button type="button" className="ph-row-more" aria-label={`${name}: ${t("dlg.menu")}`} title={t("dlg.menu")} aria-haspopup="menu"
            onClick={(e) => { e.stopPropagation(); setSheet(true); }} onPointerDown={(e) => e.stopPropagation()}>
            <Icon name="vdots" size={18} />
          </button>
        )}
      </div>
      {sheet && actions && <ActionSheet items={actions} preview={preview ?? { title, meta }} onClose={() => setSheet(false)} />}
    </div>
  );
}

/** A press on a control inside a row's body is the control's, not the row's: no open, no long press. */
function stopOnControl(e: { target: EventTarget; stopPropagation: () => void }) {
  if (e.target instanceof Element && e.target.closest("button, a, input, textarea, select")) e.stopPropagation();
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

export function SegmentedControl<T extends string>({ value, options, onChange, label, className = "" }: { value: T; options: { id: T; label: ReactNode; badge?: ReactNode }[]; onChange: (id: T) => void; label: string; className?: string }) {
  return (
    <div className={`ph-seg ${className}`} role="radiogroup" aria-label={label}>
      {options.map((option) => (
        <button key={option.id} type="button" role="radio" aria-checked={value === option.id} className={value === option.id ? "on" : ""} onClick={() => value !== option.id && onChange(option.id)}>
          <span className="ph-seg-l">{option.label}</span>
          {option.badge}
        </button>
      ))}
    </div>
  );
}

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
      {icon && <Icon name={icon} size={22} />}
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
