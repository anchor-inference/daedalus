// Notifications as they arrive: a stack of at most three in the bottom-right corner of a desktop,
// one banner at the top of a phone (the bottom of a phone holds the tab bar and the composer).
//
// Only what the host marked `toast` is shown, only when it happened now (a replay after a reconnect
// updates the lists but never pops up), never while the page is hidden, never about the session or
// terminal this window already shows (the conversation itself says it), and never on a device where
// the operator switched pop-ups off (`popups.ts`). A pop-up is a glance, not a record: every one of
// them is already in the inbox behind the bell, so letting one go, or all of them, loses nothing.
// The status line at the bottom (`dialogs.tsx`) is a different thing and is left alone.

import { useCallback, useEffect, useLayoutEffect, useReducer, useRef, useState } from "react";
import type { AppEvent, Notification } from "./api";
import { toast as statusLine } from "./ui/dialogs";
import { type EventMeta, useEvent } from "./events";
import { Icon } from "./icons";
import { setPopupsShown, usePopupsShown } from "./popups";
import { shownScopes } from "./presence";
import { navigate, pathFor } from "./router";
import { useMedia } from "./shell";
import { ActionButtons, categoryLabel, noticeIcon, openEntry, toneClass } from "./notifications";
import { plural, t } from "./i18n";

/** A notification that only reports something: long enough to read two lines twice. */
export const TOAST_MS = 7000;
/** Something went wrong or is urgent but asks nothing: a little longer, so a glance away does not miss it. */
export const URGENT_MS = 12000;
/** A request with answers on it: long enough to answer from the pop-up, but it still leaves, because
 *  the request waits in the inbox under "Needs you" for as long as it is open. */
export const ACTIONABLE_MS = 20000;
/** A pop-up that waited for room is shown only with at least this long left, so it is not a flicker. */
export const GLANCE_MS = 3000;
export const STACK_MAX = 3;
/** How far a banner is pushed up before it counts as swiped away. */
const SWIPE_PX = 40;

/** `expires`: when it leaves unless held; `held`: since when a pointer or keyboard focus rests on it. */
export type ToastItem = { entry: Notification; seq: number; expires: number; held: number | null };

/**
 * The toasts on screen and the ones waiting for room, kept apart from React so the rules are tested
 * as rules: at most `max` shown, newest on top; a repeat of an entry already here replaces it rather
 * than stacking; each one leaves at its own time whether it was shown or only waited, so a burst of
 * twenty is three pop-ups and a count for a few seconds, not a procession; and a toast's clock stops
 * only while that toast itself is held.
 *
 * The hold used to be one flag for the whole stack, set on the stack's mouseenter and cleared on its
 * mouseleave. When the last toast was closed under the pointer, the stack left the page with the
 * pointer still inside it, the mouseleave never came, and every toast after that stayed up forever;
 * a tap on a phone, which fires a mouseenter and no mouseleave, did the same; and a mouse left lying
 * in the corner held every toast that appeared under it. A hold now belongs to one toast, begins only
 * when a mouse is moved over it (see `reaching`), and leaves with it.
 */
export class ToastQueue {
  shown: ToastItem[] = [];
  waiting: ToastItem[] = [];

  constructor(public max = STACK_MAX, private now: () => number = () => Date.now()) {}

  static duration(entry: Notification): number {
    if (entry.needs_you && entry.actions.some((a) => a.id !== "open")) return ACTIONABLE_MS;
    if (entry.level === "urgent" || entry.tone === "error") return URGENT_MS;
    return TOAST_MS;
  }

  push(entry: Notification, seq: number): void {
    const expires = this.now() + ToastQueue.duration(entry);
    const here = this.shown.find((i) => i.entry.id === entry.id) ?? this.waiting.find((i) => i.entry.id === entry.id);
    if (here) {
      here.entry = entry;
      here.seq = seq;
      here.expires = expires;
      return;
    }
    this.waiting.push({ entry, seq, expires, held: null });
    this.fill();
  }

  dismiss(id: number): boolean {
    const before = this.count;
    this.shown = this.shown.filter((i) => i.entry.id !== id);
    this.waiting = this.waiting.filter((i) => i.entry.id !== id);
    this.fill();
    return this.count !== before;
  }

  /** Lets every one go, shown or waiting; returns how many there were. */
  clear(): number {
    const count = this.count;
    this.shown = [];
    this.waiting = [];
    return count;
  }

  get count(): number {
    return this.shown.length + this.waiting.length;
  }

  /** Drop what has run out; returns the next moment something will, or null. */
  tick(): number | null {
    const now = this.now();
    this.shown = this.shown.filter((i) => i.held !== null || i.expires > now);
    this.waiting = this.waiting.filter((i) => i.expires > now);
    this.fill();
    return this.next();
  }

  next(): number | null {
    const running = [...this.shown.filter((i) => i.held === null), ...this.waiting].map((i) => i.expires);
    return running.length ? Math.min(...running) : null;
  }

  /** A pointer or keyboard focus came to rest on this toast: its clock stops. */
  hold(id: number): boolean {
    const item = this.shown.find((i) => i.entry.id === id);
    if (!item || item.held !== null) return false;
    item.held = this.now();
    return true;
  }

  /** It moved away: the clock goes on from where it stopped. */
  release(id: number): void {
    const item = this.shown.find((i) => i.entry.id === id);
    if (!item || item.held === null) return;
    item.expires += this.now() - item.held;
    item.held = null;
  }

  held(id: number): boolean {
    return this.shown.some((i) => i.entry.id === id && i.held !== null);
  }

  /** Newest on top: the order the reader sees. */
  visible(): ToastItem[] {
    return [...this.shown].sort((a, b) => b.seq - a.seq);
  }

  setMax(max: number): void {
    this.max = max;
    // A window narrowed to a phone keeps the newest; the others go back in line rather than away.
    while (this.shown.length > max) {
      const oldest = this.shown.reduce((a, b) => (a.seq < b.seq ? a : b));
      this.shown = this.shown.filter((i) => i !== oldest);
      this.waiting.unshift({ ...oldest, held: null });
    }
    this.fill();
  }

  private fill(): void {
    const now = this.now();
    while (this.shown.length < this.max && this.waiting.length > 0) {
      const item = this.waiting.shift()!;
      // Too little time left to be read: it is counted until then and stays in the inbox. Were it
      // given a glance of its own, a burst that arrived a few milliseconds apart would come back one
      // by one after the first three left.
      if (item.expires - now < GLANCE_MS) continue;
      this.shown.push(item);
    }
  }
}

export type ToastContext = { visible: boolean; shown: { sessions: Set<string>; terminals: Set<string>; projects: Set<string> } };

/** Whether a `notify` event becomes a toast in this window. */
/** A phone's banner publishes its height as `--banner-h` while it shows.
 *
 *  The start screen's hero is only as tall as what it holds, so its composer sits in the top band the
 *  banner crosses: a notice arriving while the operator typed covered the field they were typing in.
 *  The start screen pads itself by this height; a chat's composer is at the foot and needs nothing. */
function useBannerRoom(stack: React.RefObject<HTMLDivElement | null>, active: boolean, count: number) {
  useLayoutEffect(() => {
    const root = document.documentElement;
    const el = stack.current;
    if (!active || !el) {
      root.style.removeProperty("--banner-h");
      return;
    }
    const publish = () => root.style.setProperty("--banner-h", `${Math.ceil(el.getBoundingClientRect().height)}px`);
    publish();
    const observer = new ResizeObserver(publish);
    observer.observe(el);
    return () => {
      observer.disconnect();
      root.style.removeProperty("--banner-h");
    };
  }, [stack, active, count]);
}

export function shouldToast(payload: AppEvent["payload"], meta: EventMeta, ctx: ToastContext): boolean {
  if (meta.replayed || !payload.toast) return false;
  const entry = payload.notification as Notification | undefined;
  if (!entry || typeof entry.id !== "number") return false;
  if (entry.level === "quiet" || entry.resolved) return false;
  if (!ctx.visible) return false;
  if (entry.session_id && ctx.shown.sessions.has(entry.session_id)) return false;
  if (entry.terminal_id && ctx.shown.terminals.has(entry.terminal_id)) return false;
  // A project-wide item (a report, a review) is attended by the project's own page.
  if (!entry.session_id && !entry.terminal_id && entry.project_id && ctx.shown.projects.has(entry.project_id)) return false;
  return true;
}

function currentContext(): ToastContext {
  return { visible: document.visibilityState === "visible", shown: shownScopes() };
}

/** Sequence numbers already raised: a stream that replays the same frame must not raise it twice. */
const RAISED_MAX = 200;

/** Whether focus arrived from the keyboard. A click focuses the button it lands on too, and that
 *  focus stays behind after the pointer has gone; only a keyboard reader's focus is a hand on the toast. */
function keyboardFocus(target: EventTarget): boolean {
  try {
    return (target as Element).matches(":focus-visible");
  } catch {
    return true;
  }
}

/** A pointer that hovers (a mouse or a pen: a finger lifts and leaves no hover behind) and is being
 *  moved over the toast. A toast that appears under a pointer resting in the corner receives an enter
 *  and even a move of no distance from the browser; counting those would hold every new toast for as
 *  long as the mouse happened to lie there, which is how pop-ups once stayed up for good. */
function reaching(e: React.PointerEvent): boolean {
  return (e.pointerType === "mouse" || e.pointerType === "pen") && (e.movementX !== 0 || e.movementY !== 0);
}

export function NotificationToasts() {
  const wide = useMedia("(min-width: 1024px)");
  const enabled = usePopupsShown();
  const queue = useRef(new ToastQueue(wide ? STACK_MAX : 1));
  const raised = useRef<number[]>([]);
  const [, redraw] = useReducer((n: number) => n + 1, 0);
  const timer = useRef<number | undefined>(undefined);

  const schedule = useCallback(() => {
    window.clearTimeout(timer.current);
    const next = queue.current.tick();
    redraw();
    if (next !== null) timer.current = window.setTimeout(schedule, Math.max(50, next - Date.now()));
  }, []);

  useEffect(() => {
    queue.current.setMax(wide ? STACK_MAX : 1);
    schedule();
  }, [wide, schedule]);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  // Switched off in another window, or in Settings while some were up: they go at once.
  useEffect(() => {
    if (!enabled && queue.current.clear()) schedule();
  }, [enabled, schedule]);

  useEvent(["notify"], (event, meta) => {
    if (!enabled || !shouldToast(event.payload, meta, currentContext())) return;
    const seq = event.seq || Date.now();
    if (event.seq) {
      if (raised.current.includes(event.seq)) return;
      raised.current = [...raised.current.slice(-RAISED_MAX + 1), event.seq];
    }
    queue.current.push(event.payload.notification as Notification, seq);
    schedule();
  });
  // Answered or read somewhere else: the toast has nothing left to say.
  useEvent(["notify.resolved"], (event) => {
    if (queue.current.dismiss(Number(event.payload.id))) schedule();
  });
  useEvent(["notify.seen"], (event) => {
    const ids = event.payload.ids;
    if (ids === "all") queue.current.clear();
    else if (Array.isArray(ids)) for (const id of ids) queue.current.dismiss(Number(id));
    schedule();
  });

  const dismiss = (id: number) => {
    queue.current.dismiss(id);
    schedule();
  };
  // Called on every move of the mouse over a toast: only the first one changes anything.
  const hold = (id: number) => {
    if (queue.current.hold(id)) schedule();
  };
  const release = (id: number) => {
    queue.current.release(id);
    schedule();
  };
  const closeAll = () => {
    queue.current.clear();
    schedule();
  };
  const openInbox = () => {
    closeAll();
    navigate(pathFor("inbox"));
  };
  const turnOff = () => {
    setPopupsShown(false);
    closeAll();
    statusLine(t("notice.popups.off"), { undo: () => setPopupsShown(true), ms: 8000 });
  };

  const stack = useRef<HTMLDivElement>(null);
  const bottom = useComposerClearance(stack, wide, queue.current.shown.length);
  const items = queue.current.visible();
  const waiting = queue.current.waiting.length;
  useBannerRoom(stack, !wide && enabled && items.length > 0, items.length);
  if (!enabled || items.length === 0) return null;
  return (
    <div
      ref={stack}
      className={`notice-toasts ${wide ? "stack" : "banner"}`}
      role="region"
      aria-label={t("notice.region")}
      style={wide && bottom !== null ? { bottom } : undefined}
    >
      {items.map((item) => (
        <Toast key={item.entry.id} entry={item.entry} onDismiss={() => dismiss(item.entry.id)} swipe={!wide} onHold={() => hold(item.entry.id)} onRelease={() => release(item.entry.id)} />
      ))}
      <div className="notice-foot">
        {waiting > 0 && (
          <button className="notice-more" onClick={openInbox} title={t("notice.more.title")}>{plural("notice.more", waiting)}</button>
        )}
        <span className="grow" />
        <button className="btn small ghost" data-popups-off onClick={turnOff} title={t("notice.popups.off.title")}>{t("notice.popups.hide")}</button>
        {items.length + waiting >= 2 && (
          <button className="btn small ghost" data-close-all onClick={closeAll}>{t("notice.closeall")}</button>
        )}
      </div>
    </div>
  );
}

function Toast({ entry, onDismiss, swipe, onHold, onRelease }: { entry: Notification; onDismiss: () => void; swipe: boolean; onHold: () => void; onRelease: () => void }) {
  const [drag, setDrag] = useState(0);
  const start = useRef<number | null>(null);
  const open = () => {
    openEntry(entry);
    onDismiss();
  };
  return (
    <div
      className={`notice-toast ${entry.level === "urgent" ? "urgent" : ""}`}
      data-notice={entry.id}
      role={entry.level === "urgent" ? "alert" : "status"}
      style={drag < 0 ? { transform: `translateY(${drag}px)`, opacity: Math.max(0.2, 1 + drag / 120) } : undefined}
      onClick={open}
      onPointerMove={(e) => { if (reaching(e)) onHold(); }}
      onPointerLeave={onRelease}
      onFocus={(e) => { if (keyboardFocus(e.target)) onHold(); }}
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) onRelease();
      }}
      onTouchStart={swipe ? (e) => { start.current = e.touches[0].clientY; } : undefined}
      onTouchMove={swipe ? (e) => { if (start.current !== null) setDrag(Math.min(0, e.touches[0].clientY - start.current)); } : undefined}
      onTouchEnd={swipe ? () => { start.current = null; if (drag < -SWIPE_PX) onDismiss(); else setDrag(0); } : undefined}
    >
      <div className="notice-toast-head">
        <span className={`kind ${toneClass(entry)}`} aria-label={categoryLabel(entry.category)}><Icon name={noticeIcon(entry)} size={14} /></span>
        <b className="notice-title clamp-2">{entry.title}</b>
        <button className="iconbtn small quiet" aria-label={t("notice.dismiss")} title={t("notice.dismiss")} onClick={(e) => { e.stopPropagation(); onDismiss(); }}>
          <Icon name="close" size={14} />
        </button>
      </div>
      {entry.body && <div className="notice-body clamp-2">{entry.body}</div>}
      <ActionButtons entry={entry} onDone={onDismiss} />
    </div>
  );
}

/**
 * How high the stack must sit to stay clear of a composer under it. A small laptop puts the
 * conversation's composer right across the bottom-right corner, and a toast drawn over the field the
 * operator is typing in, or over its send button, is worse than no toast; so the stack rises above
 * whichever composer it would cover. Null: the corner is free.
 *
 * Measured after every drawing of the stack and whenever a composer changes size, not only when the
 * window does: the composer grows line by line while a message is written, and a screen opened under
 * a standing stack brings a composer of its own.
 */
function useComposerClearance(stack: React.RefObject<HTMLDivElement | null>, wide: boolean, count: number): number | null {
  const [bottom, setBottom] = useState<number | null>(null);
  const measure = useCallback(() => {
    const box = stack.current?.getBoundingClientRect();
    const left = box ? box.left : window.innerWidth - 376;
    // Where the stack sits when nothing lifts it. A composer counts only when it reaches into that
    // band: the start screen's composer stands in the middle of the window, and lifting the stack
    // above it — because it reached the right-hand column at all — left the toasts floating in the
    // middle of the screen with nothing under them.
    const floor = window.innerHeight - 16;
    const ceiling = floor - (box ? box.height : 0);
    let top = Number.POSITIVE_INFINITY;
    for (const el of document.querySelectorAll<HTMLElement>(".composer-box")) {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.right > left && r.bottom > ceiling) top = Math.min(top, r.top);
    }
    const next = Number.isFinite(top) ? Math.round(window.innerHeight - top + 12) : null;
    setBottom((cur) => (cur === next ? cur : next));
  }, [stack]);
  useLayoutEffect(() => {
    if (wide && count > 0) measure();
  });
  useEffect(() => {
    if (!wide || count === 0) return;
    window.addEventListener("resize", measure);
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    for (const el of document.querySelectorAll<HTMLElement>(".composer-box")) observer?.observe(el);
    return () => {
      window.removeEventListener("resize", measure);
      observer?.disconnect();
    };
  }, [wide, count, measure]);
  return bottom;
}
