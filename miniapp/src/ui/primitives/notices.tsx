// The pop-ups that report what happened while the operator was elsewhere: a stack in a desktop's corner
// (three at a time, newest on top, the rest counted), one banner at the top of a phone. They share the
// status line's host (toast.tsx) and its Base UI parts, but not its one-line-at-a-time rule: a notice
// queues, holds under a pointer, leaves by a swipe, and carries the buttons that answer it.
//
// The queue is kept apart from React so its rules are tested as rules: at most `max` shown, a repeat of
// a notice already here replaces it rather than stacking, each one leaves at its own time whether it was
// shown or only waited, so a burst of twenty is three pop-ups and a count for a few seconds, not a
// procession; and a notice's clock stops only while that notice itself is held.
//
// The hold used to be one flag for the whole stack, set on the stack's mouseenter and cleared on its
// mouseleave. When the last notice was closed under the pointer, the stack left the page with the
// pointer still inside it, the mouseleave never came, and every notice after that stayed up forever;
// a tap on a phone, which fires a mouseenter and no mouseleave, did the same; and a mouse left lying
// in the corner held every notice that appeared under it. A hold now belongs to one notice, begins only
// when a mouse is moved over it (see `reaching`), and leaves with it.

import { Toast } from "@base-ui/react/toast";
import { useCallback, useEffect, useLayoutEffect, useState, useSyncExternalStore, type CSSProperties, type ReactNode, type RefObject } from "react";
import { Icon } from "../../icons";
import { t, plural } from "../../i18n";

/** A pop-up that waited for room is shown only with at least this long left, so it is not a flicker. */
export const GLANCE_MS = 3000;
export const STACK_MAX = 3;

/** `expires`: when it leaves unless held; `held`: since when a pointer or keyboard focus rests on it. */
export type NoticeItem<T> = { key: number; data: T; seq: number; expires: number; held: number | null };

export class NoticeQueue<T> {
  shown: NoticeItem<T>[] = [];
  waiting: NoticeItem<T>[] = [];

  constructor(public max = STACK_MAX, private now: () => number = () => Date.now()) {}

  /** `replaces` says which notice already here this one is a repeat of, besides one with its own key:
      a failed /compact is told by its own answer and by the host's notification about the same summary
      model, and two pop-ups saying it was one too many. */
  push(key: number, data: T, seq: number, ms: number, replaces?: (other: T) => boolean): void {
    const expires = this.now() + ms;
    const same = (i: NoticeItem<T>) => i.key === key || (!!replaces && replaces(i.data));
    const here = this.shown.find(same) ?? this.waiting.find(same);
    if (here) {
      here.data = data;
      here.seq = seq;
      here.expires = expires;
      return;
    }
    this.waiting.push({ key, data, seq, expires, held: null });
    this.fill();
  }

  dismiss(key: number): boolean {
    const before = this.count;
    this.shown = this.shown.filter((i) => i.key !== key);
    this.waiting = this.waiting.filter((i) => i.key !== key);
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

  /** A pointer or keyboard focus came to rest on this notice: its clock stops. */
  hold(key: number): boolean {
    const item = this.shown.find((i) => i.key === key);
    if (!item || item.held !== null) return false;
    item.held = this.now();
    return true;
  }

  /** It moved away: the clock goes on from where it stopped. */
  release(key: number): void {
    const item = this.shown.find((i) => i.key === key);
    if (!item || item.held === null) return;
    item.expires += this.now() - item.held;
    item.held = null;
  }

  held(key: number): boolean {
    return this.shown.some((i) => i.key === key && i.held !== null);
  }

  /** Newest on top: the order the reader sees. */
  visible(): NoticeItem<T>[] {
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
      // Too little time left to be read: it is counted until then and stays wherever the caller keeps
      // the record. Were it given a glance of its own, a burst that arrived a few milliseconds apart
      // would come back one by one after the first three left.
      if (item.expires - now < GLANCE_MS) continue;
      this.shown.push(item);
    }
  }
}

/** What a notice draws. The caller builds the pieces (its icon, the buttons that answer it), the host
    arranges them. */
export type NoticeView = {
  title: ReactNode;
  body?: ReactNode;
  /** The glyph's own wrapper, with the tone's colour on it. */
  icon?: ReactNode;
  /** Draws the pop-up as an alert, which a screen reader reads out at once. */
  urgent?: boolean;
  /** `warning` or `error` tint the glyph the way the notice's tone says. */
  tone?: string;
  /** Under the text: the buttons that answer it or take the reader to what it is about. */
  actions?: ReactNode;
  /** Pressed anywhere on the pop-up that is not a button. */
  onOpen: () => void;
  /** Said by the screen reader beside the title: the category of what happened. */
  group?: string;
};

export type NoticeFoot = { onOpenWaiting: () => void; onTurnOff: () => void; onCloseAll: () => void };

type Snapshot = { shown: NoticeItem<NoticeView>[]; waiting: number };

const queue = new NoticeQueue<NoticeView>(STACK_MAX);
let snapshot: Snapshot = { shown: [], waiting: 0 };
let footHandlers: NoticeFoot | null = null;
let timer: number | undefined;
const listeners = new Set<() => void>();
const manager = Toast.createToastManager<{ key: number }>();
const idOf = (key: number) => `notice-${key}`;

function publish() {
  snapshot = { shown: queue.visible(), waiting: queue.waiting.length };
  listeners.forEach((l) => l());
}

/** Drops what ran out, tells the host, and sets the next wake-up for the moment something else will. */
function schedule() {
  window.clearTimeout(timer);
  const next = queue.tick();
  publish();
  if (next !== null) timer = window.setTimeout(schedule, Math.max(50, next - Date.now()));
}

/** Raises a notice. A repeat of one already here (same `key`, or what `replaces` names) replaces it. */
export function raiseNotice(notice: { key: number; seq: number; ms: number; view: NoticeView; replaces?: (other: NoticeView) => boolean }): void {
  queue.push(notice.key, notice.view, notice.seq, notice.ms, notice.replaces);
  schedule();
}

export function dismissNotice(key: number): boolean {
  const gone = queue.dismiss(key);
  if (gone) schedule();
  return gone;
}

/** Lets every one go, the waiting ones too; returns how many there were. */
export function clearNotices(): number {
  const count = queue.clear();
  if (count) schedule();
  return count;
}

export function noticesWaiting(): number {
  return queue.waiting.length;
}

/** Which buttons the foot line's three words press: set by whoever knows what they open. */
export function setNoticeFoot(handlers: NoticeFoot | null): void {
  footHandlers = handlers;
}

function hold(key: number) {
  if (queue.hold(key)) schedule();
}

function release(key: number) {
  queue.release(key);
  schedule();
}

/** A phone's banner publishes its height as `--banner-h` while it shows.

    The start screen's hero is only as tall as what it holds, so its composer sits in the top band the
    banner crosses: a notice arriving while the operator typed covered the field they were typing in.
    The start screen pads itself by this height; a chat's composer is at the foot and needs nothing. */
function useBannerRoom(stack: RefObject<HTMLDivElement | null>, active: boolean, count: number) {
  useLayoutEffect(() => {
    const root = document.documentElement;
    const el = stack.current;
    if (!active || !el) {
      root.style.removeProperty("--banner-h");
      return;
    }
    const publishHeight = () => root.style.setProperty("--banner-h", `${Math.ceil(el.getBoundingClientRect().height)}px`);
    publishHeight();
    const observer = new ResizeObserver(publishHeight);
    observer.observe(el);
    return () => {
      observer.disconnect();
      root.style.removeProperty("--banner-h");
    };
  }, [stack, active, count]);
}

/**
 * How high the stack must sit to stay clear of a composer under it. A small laptop puts the
 * conversation's composer right across the bottom-right corner, and a pop-up drawn over the field the
 * operator is typing in, or over its send button, is worse than no pop-up; so the stack rises above
 * whichever composer it would cover. Null: the corner is free.
 *
 * Measured after every drawing of the stack and whenever a composer changes size, not only when the
 * window does: the composer grows line by line while a message is written, and a screen opened under
 * a standing stack brings a composer of its own.
 */
function useComposerClearance(stack: RefObject<HTMLDivElement | null>, wide: boolean, count: number): number | null {
  const [bottom, setBottom] = useState<number | null>(null);
  const measure = useCallback(() => {
    const box = stack.current?.getBoundingClientRect();
    const left = box ? box.left : window.innerWidth - 376;
    // Where the stack sits when nothing lifts it. A composer counts only when it reaches into that
    // band: the start screen's composer stands in the middle of the window, and lifting the stack
    // above it — because it reached the right-hand column at all — left the pop-ups floating in the
    // middle of the screen with nothing under them.
    const floor = window.innerHeight - 24;
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

/** Whether focus arrived from the keyboard. A click focuses the button it lands on too, and that
    focus stays behind after the pointer has gone; only a keyboard reader's focus is a hand on the pop-up. */
function keyboardFocus(target: EventTarget): boolean {
  try {
    return (target as Element).matches(":focus-visible");
  } catch {
    return true;
  }
}

/** A pointer that hovers (a mouse or a pen: a finger lifts and leaves no hover behind) and is being
    moved over the pop-up. One that appears under a pointer resting in the corner receives an enter
    and even a move of no distance from the browser; counting those would hold every new pop-up for as
    long as the mouse happened to lie there, which is how pop-ups once stayed up for good. */
function reaching(e: { pointerType: string; movementX: number; movementY: number }): boolean {
  return (e.pointerType === "mouse" || e.pointerType === "pen") && (e.movementX !== 0 || e.movementY !== 0);
}

/** Whether the window is wide enough for the stack in the corner. Read here rather than through the
    shell's `useMedia`: the shell reaches every screen, and the modules that raise a toast are read by
    code that runs without a window. */
function useDesktop(): boolean {
  const query = "(min-width: 1024px)";
  const [on, setOn] = useState(() => window.matchMedia?.(query).matches ?? false);
  useEffect(() => {
    const mq = window.matchMedia?.(query);
    if (!mq) return;
    const update = () => setOn(mq.matches);
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, []);
  return on;
}

const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
};

/**
 * The stack, or the banner: drawn inside the status line's host. The queue decides what is shown;
 * Base UI's toast manager holds what the queue shows, so each pop-up has its root's role, its swipe
 * and the viewport's landmark.
 */
export function NoticeStack() {
  const wide = useDesktop();
  const { shown, waiting } = useSyncExternalStore(subscribe, () => snapshot);
  useEffect(() => {
    queue.setMax(wide ? STACK_MAX : 1);
    schedule();
  }, [wide]);
  return (
    <Toast.Provider toastManager={manager} limit={STACK_MAX + 2}>
      <Stack wide={wide} shown={shown} waiting={waiting} />
    </Toast.Provider>
  );
}

function Stack({ wide, shown, waiting }: { wide: boolean; shown: NoticeItem<NoticeView>[]; waiting: number }) {
  const [stack, setStack] = useState<HTMLDivElement | null>(null);
  const ref = { current: stack } as RefObject<HTMLDivElement | null>;
  const bottom = useComposerClearance(ref, wide, shown.length);
  useBannerRoom(ref, !wide && shown.length > 0, shown.length);
  const { toasts, add, close } = Toast.useToastManager<{ key: number }>();
  // The manager follows the queue: what the queue shows is added, what it dropped is closed. A swipe
  // closes the root, and the queue hears it from `onClose`.
  useEffect(() => {
    const wanted = new Set(shown.map((s) => idOf(s.key)));
    for (const each of toasts) if (!wanted.has(each.id) && each.transitionStatus !== "ending") close(each.id);
    for (const s of shown) {
      if (!toasts.some((each) => each.id === idOf(s.key) && each.transitionStatus !== "ending")) add({ id: idOf(s.key), title: "", timeout: 0, type: "notice", data: { key: s.key }, onClose: () => { if (queue.dismiss(s.key)) schedule(); } });
    }
  }, [shown, toasts, add, close]);
  if (shown.length === 0) return null;
  const foot = footHandlers;
  const style: CSSProperties | undefined = wide && bottom !== null ? { bottom } : undefined;
  return (
    <Toast.Portal>
      <Toast.Viewport ref={setStack} className={`notice-toasts ${wide ? "stack" : "banner"}`} aria-label={t("notice.region")} style={style}>
        {shown.map((item) => {
          const root = toasts.find((each) => each.id === idOf(item.key) && each.transitionStatus !== "ending");
          return root ? <Notice key={item.key} itemKey={item.key} view={item.data} toastObject={root} swipe={wide ? "right" : "up"} /> : null;
        })}
        <div className="notice-foot">
          {waiting > 0 && (
            <button type="button" className="notice-more" onClick={() => foot?.onOpenWaiting()} title={t("notice.more.title")}>{plural("notice.more", waiting)}</button>
          )}
          <span className="grow" />
          {/* Plain buttons in the classes of `Button` and `IconButton`: those draw from controls.tsx, which
              reaches the router, and the module that raises notices is read by code that runs without a window. */}
          <button type="button" className="btn small ghost" data-popups-off="" onClick={() => foot?.onTurnOff()} title={t("notice.popups.off.title")}>{t("notice.popups.hide")}</button>
          {shown.length + waiting >= 2 && (
            <button type="button" className="btn small ghost" data-close-all="" onClick={() => foot?.onCloseAll()}>{t("notice.closeall")}</button>
          )}
        </div>
      </Toast.Viewport>
    </Toast.Portal>
  );
}

function Notice({ itemKey, view, toastObject, swipe }: { itemKey: number; view: NoticeView; toastObject: Parameters<typeof Toast.Root>[0]["toast"]; swipe: "right" | "up" }) {
  return (
    <Toast.Root
      toast={toastObject}
      className={`notice-toast ${view.urgent ? "urgent" : ""} ${view.tone ? `tone-${view.tone}` : ""}`.replace(/\s+/g, " ").trim()}
      data-notice={itemKey}
      role={view.urgent ? "alert" : "status"}
      swipeDirection={swipe}
      onClick={(e) => { if (!(e.target as HTMLElement).closest("button, a")) view.onOpen(); }}
      onPointerMove={(e) => { if (reaching(e)) hold(itemKey); }}
      onPointerLeave={() => release(itemKey)}
      onFocus={(e) => { if (keyboardFocus(e.target)) hold(itemKey); }}
      onBlur={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node | null)) release(itemKey); }}
    >
      <div className="notice-toast-head">
        {view.icon}
        <b className="notice-title clamp-2">{view.title}</b>
        <button type="button" className="iconbtn small quiet" aria-label={t("notice.dismiss")} title={t("notice.dismiss")} onClick={() => { if (queue.dismiss(itemKey)) schedule(); }}>
          <Icon name="close" size={16} />
        </button>
      </div>
      {view.body && <div className="notice-body clamp-2">{view.body}</div>}
      {view.actions}
    </Toast.Root>
  );
}

/** The glyph a notice leads with: 16 px, in the tone's colour. */
export function NoticeGlyph({ name, className, label }: { name: Parameters<typeof Icon>[0]["name"]; className?: string; label?: string }) {
  return <span className={`kind ${className ?? ""}`.trim()} aria-label={label}><Icon name={name} size={16} /></span>;
}
