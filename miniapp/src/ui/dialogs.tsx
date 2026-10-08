// The temporary layers: a sheet (bottom drawer on a phone, dialog on a desktop), a confirmation
// that says what happens, an overflow menu, and a toast that can undo. Escape closes the top one,
// focus goes in and comes back to the control that opened it.

import { useContextActions } from "./context-menu";
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useMoreBelow } from "../edgefade";
import { Icon, IconName } from "../icons";
import { t } from "../i18n";

// ── sheet ────────────────────────────────────────────────────────────────────────────────

/** The layers open right now, top last: Escape goes to the top one only.

    The key is read in the capture phase. Every layer is drawn through `Overlay`, whose root stops
    events so a click in a sheet never reaches the row that opened it — and that stop happens at
    the portal's container, before the document's bubble phase. A listener there never heard the
    key at all; capture runs first. */
const layers: symbol[] = [];
export function useLayer(onEscape: () => void) {
  const cb = useRef(onEscape);
  cb.current = onEscape;
  useEffect(() => {
    const me = Symbol("layer");
    layers.push(me);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && layers[layers.length - 1] === me) {
        e.preventDefault();
        cb.current();
      }
    };
    document.addEventListener("keydown", onKey, { capture: true });
    return () => {
      document.removeEventListener("keydown", onKey, { capture: true });
      const i = layers.indexOf(me);
      if (i >= 0) layers.splice(i, 1);
    };
  }, []);
}

export function trapDialogTab(e: React.KeyboardEvent<HTMLElement>) {
  if (e.key !== "Tab") return;
  const items = Array.from(e.currentTarget.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), a[href], [tabindex='0']")).filter((el) => el.getClientRects().length > 0);
  if (!items.length) { e.preventDefault(); return; }
  const index = items.indexOf(document.activeElement as HTMLElement);
  if (e.shiftKey && index <= 0) { e.preventDefault(); items[items.length - 1].focus(); }
  else if (!e.shiftKey && (index < 0 || index === items.length - 1)) { e.preventDefault(); items[0].focus(); }
}

export function Sheet({ title, ariaLabel, onClose, children, size, className, head }: { title?: ReactNode; ariaLabel?: string; onClose: () => void; children: ReactNode; size?: "wide" | "narrow" | "full"; className?: string; head?: ReactNode }) {
  const panel = useRef<HTMLDivElement>(null);
  const body = useRef<HTMLDivElement>(null);
  useMoreBelow(body);
  useLayer(onClose);
  // Focus moves in once, on open, and back to the control that opened the sheet when it closes;
  // the callback's identity changes on every parent render and must not re-run this.
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    panel.current?.focus();
    return () => {
      opener?.focus?.();
    };
  }, []);
  return (
    <Overlay>
      <div className="sheet-backdrop" onClick={(e) => { e.stopPropagation(); onClose(); }}>
      <div ref={panel} tabIndex={-1} className={`sheet ${size ?? ""} ${className ?? ""}`} onClick={(e) => e.stopPropagation()} onKeyDown={trapDialogTab} role="dialog" aria-modal="true" aria-label={typeof title === "string" ? title : ariaLabel}>
        <div className="grip" />
        <div className="sheet-head">
          {title && <h3>{title}</h3>}
          {head}
          <button className="iconbtn small" onClick={onClose} aria-label={t("common.close")} title={t("common.close")}><Icon name="close" size={16} /></button>
        </div>
        <div ref={body} className="sheet-body">{children}</div>
        {/* Shown by the stylesheet only while the body has more below its fold (data-more) and no
            sticky footer: a touch screen draws no scrollbar, and a task sheet whose actions sat under
            the fold ended at a clean edge that looked like its end. A tap scrolls most of a screen on. */}
        <div className="sheet-more-anchor">
          <button type="button" className="sheet-more" tabIndex={-1} aria-label={t("common.more.below")} title={t("common.more.below")} onClick={() => body.current?.scrollBy({ top: body.current.clientHeight * 0.8, behavior: "smooth" })}>
            <Icon name="chevron" size={16} />
          </button>
        </div>
      </div>
      </div>
    </Overlay>
  );
}

/** Every overlay is rendered in the document, never where it was declared.

    An ancestor with a transform, a filter or `contain` becomes the containing block of a
    `position: fixed` descendant, and its `overflow: hidden` clips it. A sheet declared inside a
    pressable card (`:active { transform: scale(…) }`) therefore snapped to the card under the
    pressed mouse button and lost its body to the card's clip; the release landed outside the
    control, so the choice never registered. React still bubbles events through a portal to the
    component tree, so the backdrops stop their clicks and key presses from reaching the row that
    declared them — a row that opens on click must not open under its own dialog. */
export function Overlay({ children }: { children: ReactNode }) {
  return createPortal(
    <div className="overlay-root" onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()} onMouseDown={(e) => e.stopPropagation()} onPointerDown={(e) => e.stopPropagation()}>
      {children}
    </div>,
    document.body,
  );
}

// ── confirm ──────────────────────────────────────────────────────────────────────────────

export type ConfirmOptions = { title: string; body?: ReactNode; action?: string; cancel?: string; danger?: boolean };

type Pending = ConfirmOptions & { resolve: (ok: boolean) => void };
let pendingSetter: ((f: (cur: Pending | null) => Pending | null) => void) | null = null;

/** Asks before something irreversible: the title names the action, the body says what it does. */
export function confirmDialog(opts: ConfirmOptions): Promise<boolean> {
  return new Promise((resolve) => {
    if (!pendingSetter) {
      resolve(window.confirm(opts.title));
      return;
    }
    // A second question while one is open answers the first with "no" rather than leaving it hanging.
    pendingSetter((cur) => {
      cur?.resolve(false);
      return { ...opts, resolve };
    });
  });
}

export function ConfirmHost() {
  const [pending, setPending] = useState<Pending | null>(null);
  useEffect(() => {
    pendingSetter = setPending;
    return () => {
      pendingSetter = null;
    };
  }, []);
  if (!pending) return null;
  return <ConfirmDialog pending={pending} onDone={(ok) => { pending.resolve(ok); setPending(null); }} />;
}

function ConfirmDialog({ pending, onDone }: { pending: Pending; onDone: (ok: boolean) => void }) {
  const box = useRef<HTMLDivElement>(null);
  useLayer(() => onDone(false));
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    // The safe button takes focus when the action destroys something; Tab stays inside the dialog.
    const buttons = box.current?.querySelectorAll<HTMLButtonElement>("button") ?? [];
    (pending.danger ? buttons[0] : buttons[1])?.focus();
    return () => opener?.focus?.();
  }, [pending.danger]);
  const trap = (e: React.KeyboardEvent) => {
    if (e.key !== "Tab") return;
    const buttons = Array.from(box.current?.querySelectorAll<HTMLButtonElement>("button") ?? []);
    if (!buttons.length) return;
    const i = buttons.indexOf(document.activeElement as HTMLButtonElement);
    const next = e.shiftKey ? buttons[(i - 1 + buttons.length) % buttons.length] : buttons[(i + 1) % buttons.length];
    next.focus();
    e.preventDefault();
  };
  return (
    <Overlay>
    <div className="sheet-backdrop confirm" onClick={() => onDone(false)}>
      <div ref={box} className="dialog" role="alertdialog" aria-modal="true" aria-labelledby="confirm-title" onClick={(e) => e.stopPropagation()} onKeyDown={trap}>
        <h3 id="confirm-title">{pending.title}</h3>
        {pending.body && <div className="dialog-body">{pending.body}</div>}
        <div className="dialog-actions">
          {/* A framed button like its neighbour, not a bare word: beside a filled "Delete task" the
              way back read as the smaller, lesser choice, and on a touch screen looked like no target. */}
          <button className="btn" onClick={() => onDone(false)}>{pending.cancel ?? t("common.cancel")}</button>
          <button className={`btn ${pending.danger ? "danger solid" : "primary"}`} onClick={() => onDone(true)}>
            {pending.action ?? t("common.ok")}
          </button>
        </div>
      </div>
    </div>
    </Overlay>
  );
}

// ── overflow menu ────────────────────────────────────────────────────────────────────────

/** `warn` marks an item that reaches outside the sandbox (a terminal on the machine itself): amber, not red.
 * `checked` makes the item a checkbox, which keeps the menu open: it sets how the items below it act. */
export type MenuItem = { label: string; icon?: IconName; danger?: boolean; warn?: boolean; disabled?: boolean; hint?: string; checked?: boolean; onSelect: () => void } | "-";

/** With `trigger`, the button is that content (a title with a chevron) rather than an icon. */
export function OverflowMenu({ items, label, icon = "more", small, className, trigger: customTrigger, contextSelector }: { contextSelector?: string; items: MenuItem[]; label?: string; icon?: IconName; small?: boolean; className?: string; trigger?: ReactNode }) {
  const name = label ?? t("dlg.menu");
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  useContextActions(trigger, items, contextSelector);
  const [pos, setPos] = useState<{ top?: number; bottom?: number; right?: number; left?: number } | null>(null);
  /** Put the menu against the trigger as it stands now. Called again on the scroll that opened it. */
  const place = useCallback(() => {
    if (!trigger.current) return;
    const r = trigger.current.getBoundingClientRect();
    // Which edge the menu hangs from is the trigger's own position, not a habit. A control near the
    // right edge opens leftward; one near the left edge — the session's title sits there, against the
    // sidebar — opens rightward, because a menu hung from its right edge would run under the sidebar
    // and lose its first characters.
    const width = menu.current?.offsetWidth ?? 260;
    const side = r.right - width < 8 ? { left: Math.max(8, Math.min(r.left, window.innerWidth - width - 8)) } : { right: Math.max(8, window.innerWidth - r.right) };
    // A control in the lower half opens its menu upward: a menu that runs off the bottom edge has to
    // be scrolled to, and the scroll is what closes it.
    setPos(r.top > window.innerHeight / 2 ? { bottom: window.innerHeight - r.top + 4, ...side } : { top: r.bottom + 4, ...side });
  }, []);
  useLayoutEffect(() => {
    if (open) place();
  }, [open, place]);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent | TouchEvent) => {
      if (menu.current?.contains(e.target as Node) || trigger.current?.contains(e.target as Node)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      const buttons = Array.from(menu.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") ?? []);
      if (buttons.length === 0) return;
      const i = buttons.indexOf(document.activeElement as HTMLButtonElement);
      // Tab is part of the ring, not a way out of it: this menu carries the app's navigation, and a
      // Tab that walked into the page behind it would leave an open menu the reader cannot see.
      const to =
        e.key === "ArrowDown" || (e.key === "Tab" && !e.shiftKey) ? buttons[(i + 1) % buttons.length]
        : e.key === "ArrowUp" || (e.key === "Tab" && e.shiftKey) ? buttons[(i - 1 + buttons.length) % buttons.length]
        : e.key === "Home" ? buttons[0]
        : e.key === "End" ? buttons[buttons.length - 1]
        : null;
      if (!to) return;
      to.focus();
      e.preventDefault();
    };
    // A scroll closes the menu — except the one that brought the control into view a frame ago:
    // the browser reports that scroll after the click that opened the menu, not before it.
    const openedAt = performance.now();
    const onScroll = () => {
      // ... and that one moves the menu with its control instead of being ignored: the coordinates
      // were measured before the scroll, and a fixed box left at them floats away from the trigger
      // and stays there.
      if (performance.now() - openedAt > 200) setOpen(false);
      else place();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("touchstart", onDown);
    document.addEventListener("keydown", onKey);
    window.addEventListener("scroll", onScroll, { capture: true });
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("touchstart", onDown);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", onScroll, { capture: true });
      // The menu is gone; a keyboard reader continues from the control that opened it.
      if (document.activeElement === document.body || !document.activeElement) trigger.current?.focus();
    };
  }, [open, place]);
  // The menu exists only once it has somewhere to be, which is a render later than the open: taking
  // the focus before that is taking it from nothing, and the reader stays on the trigger.
  useEffect(() => {
    if (open && pos) (menu.current?.querySelector("button:not(:disabled)") as HTMLButtonElement | null)?.focus();
  }, [open, pos]);
  return (
    <>
      <button ref={trigger} className={`${customTrigger ? "" : `iconbtn ${small ? "small" : ""}`} ${open ? "on" : ""} ${className ?? ""}`} aria-label={name} title={name} aria-haspopup="menu" aria-expanded={open} onClick={(e) => { e.stopPropagation(); setOpen((o) => !o); }}>
        {customTrigger ?? <Icon name={icon} size={small ? 16 : 18} />}
      </button>
      {open && pos && createPortal(
        // In the document's own stacking context, not the row's: a `position: fixed` menu inside an
        // element that gets a transform (a card with `:active { transform: scale(…) }` under the
        // pressing finger) is positioned against that element instead of the viewport. The menu then
        // jumps away between mousedown and mouseup, the release lands outside it, and no click ever
        // reaches the item — every action in the menu looked dead.
        <div ref={menu} className="menu" role="menu" style={{ position: "fixed", top: pos.top ?? "auto", bottom: pos.bottom, right: pos.right, left: pos.left }} onClick={(e) => e.stopPropagation()}>
          <MenuLayer onClose={() => setOpen(false)} />
          {items.map((it, i) =>
            it === "-" ? (
              <div key={i} className="menu-sep" />
            ) : (
              <button key={i} role={it.checked === undefined ? "menuitem" : "menuitemcheckbox"} aria-checked={it.checked} className={it.danger ? "danger" : it.warn ? "warn" : ""} disabled={it.disabled} title={it.hint} onClick={() => { if (it.checked === undefined) setOpen(false); it.onSelect(); }}>
                {it.icon && <Icon name={it.icon} size={16} />}
                {it.label}
                {it.checked !== undefined && <span className={`menu-check ${it.checked ? "on" : ""}`} aria-hidden="true">{it.checked && <Icon name="check" size={14} />}</span>}
              </button>
            ),
          )}
        </div>,
        document.body,
      )}
    </>
  );
}

/** The open menu is a layer like a sheet: Escape reaches it first, and only it. */
function MenuLayer({ onClose }: { onClose: () => void }) {
  useLayer(onClose);
  return null;
}

// ── popover ──────────────────────────────────────────────────────────────────────────────

/**
 * A small menu anchored to a control and opening upward from it: the composer's `+` and its model
 * list. Left-aligned to the control rather than right-aligned like the overflow menu, because the
 * controls it serves sit at the left edge of the pill, where a right-aligned menu leaves the window.
 * A layer for Escape, a click outside closes it, arrows move between its buttons, focus goes to the
 * first one and comes back to the control after.
 */
export function Popover({ anchor, onClose, children, className, align = "left", label }: { anchor: HTMLElement | null; onClose: () => void; children: ReactNode; className?: string; align?: "left" | "right"; label?: string }) {
  const box = useRef<HTMLDivElement>(null);
  useLayer(onClose);
  const [pos, setPos] = useState<{ top: number; left: number; maxHeight: number } | null>(null);
  useLayoutEffect(() => {
    if (!anchor) return;
    const place = () => {
      const r = anchor.getBoundingClientRect();
      const width = box.current?.getBoundingClientRect().width ?? 340;
      const above = r.top - 14;
      const below = window.innerHeight - r.bottom - 14;
      const upward = above >= below;
      const maxHeight = Math.max(40, upward ? above : below);
      const height = Math.min(box.current?.scrollHeight ?? 0, maxHeight);
      const left = Math.min(window.innerWidth - width - 8, Math.max(8, align === "left" ? r.left : r.right - width));
      setPos({ left: Math.max(8, left), top: upward ? Math.max(8, r.top - height - 6) : r.bottom + 6, maxHeight });
    };
    place();
    const observer = new ResizeObserver(place);
    if (box.current) observer.observe(box.current);
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => { observer.disconnect(); window.removeEventListener("resize", place); window.removeEventListener("scroll", place, true); };
  }, [anchor, align]);
  useEffect(() => {
    const onDown = (e: MouseEvent | TouchEvent) => {
      if (box.current?.contains(e.target as Node) || anchor?.contains(e.target as Node)) return;
      onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented) return;
      if (e.key === "Tab") { onClose(); return; }
      if (e.target instanceof HTMLInputElement && e.target.type !== "radio" && e.key !== "ArrowDown") return;
      if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) return;
      const buttons = Array.from(box.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") ?? []);
      if (!buttons.length) return;
      const i = buttons.indexOf(document.activeElement as HTMLButtonElement);
      const next = e.key === "Home" ? buttons[0] : e.key === "End" ? buttons[buttons.length - 1] : e.key === "ArrowDown" ? buttons[(i + 1) % buttons.length] : buttons[(i - 1 + buttons.length) % buttons.length];
      next.focus();
      e.preventDefault();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("touchstart", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("touchstart", onDown);
      document.removeEventListener("keydown", onKey);
      if (document.activeElement === document.body || !document.activeElement) anchor?.focus();
    };
  }, [anchor, onClose]);
  // The first control takes the keyboard once the box is placed. Focused any earlier, it is still
  // `visibility: hidden` and the browser refuses: the focus stayed on the button that opened the menu,
  // and the arrow keys started from nowhere.
  const placed = pos !== null;
  useEffect(() => {
    if (placed) box.current?.querySelector<HTMLElement>(".model-search, [aria-checked='true'], input:checked, button[aria-current='true'], button:not(:disabled), input, [role='slider']")?.focus();
  }, [placed]);
  return createPortal(
    <div ref={box} className={`menu pop ${className ?? ""}`} role="menu" aria-label={label} style={{ position: "fixed", top: pos?.top ?? 8, bottom: "auto", left: pos?.left ?? 8, right: "auto", maxHeight: pos?.maxHeight, visibility: pos ? "visible" : "hidden" }} onClick={(e) => e.stopPropagation()}>
      {children}
    </div>,
    document.body,
  );
}

// ── toast ────────────────────────────────────────────────────────────────────────────────

type ToastState = { text: string; undo?: () => void; action?: { label: string; run: () => void }; id: number };
type ToastUpdate = (f: (cur: ToastState | null) => ToastState | null) => void;
let toastSetter: ToastUpdate | null = null;
let toastSeq = 0;

/** A short status line at the bottom; with `undo`, a button that calls it before the timer runs out,
 *  and with `action`, a button of its own words (a browser that opened offers "View"). */
export function toast(text: string, opts: { undo?: () => void; action?: { label: string; run: () => void }; ms?: number } = {}): number {
  if (!toastSetter) return 0;
  const id = ++toastSeq;
  toastSetter(() => ({ text, undo: opts.undo, action: opts.action, id }));
  window.setTimeout(() => toastSetter?.((cur) => (cur && cur.id === id ? null : cur)), opts.ms ?? (opts.undo || opts.action ? 6000 : 2600));
  return id;
}

/** Takes one toast away early, if it is still the one on screen: its offer was taken another way. */
export function dismissToast(id: number): void {
  toastSetter?.((cur) => (cur && cur.id === id ? null : cur));
}

export function ToastHost() {
  const [state, setState] = useState<ToastState | null>(null);
  useEffect(() => {
    toastSetter = setState;
    return () => {
      toastSetter = null;
    };
  }, []);
  if (!state) return null;
  return (
    <div className="toast" role="status" aria-live="polite">
      <span>{state.text}</span>
      {state.undo && (
        <button className="btn small ghost" onClick={() => { state.undo?.(); setState(null); }}>
          {t("common.undo")}
        </button>
      )}
      {state.action && (
        <button className="btn small ghost toast-action" onClick={() => { state.action?.run(); setState(null); }}>
          {state.action.label}
        </button>
      )}
    </div>
  );
}

const UNDO_MS = 5000;

/**
 * Deletes after a pause: the row disappears at once, the toast offers Undo for exactly as long as
 * the request is held back, and the request goes out only when the pause ends without one.
 */
export function deleteWithUndo(label: string, commit: () => Promise<void>, onUndo: () => void, onFail: (e: unknown) => void): void {
  let undone = false;
  const timer = window.setTimeout(() => {
    toastSetter?.((cur) => (cur && cur.text === label ? null : cur));
    commit().catch((e) => {
      onUndo();
      onFail(e);
    });
  }, UNDO_MS);
  toast(label, {
    ms: UNDO_MS,
    undo: () => {
      undone = true;
      window.clearTimeout(timer);
      onUndo();
    },
  });
  void undone;
}
