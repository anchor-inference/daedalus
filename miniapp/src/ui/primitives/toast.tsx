// The one line for what would otherwise go unnoticed: a failure, a refusal, an undo. Base UI's toast
// manager holds it, so a module far from any component can raise one, and the host draws it.

import { Toast } from "@base-ui/react/toast";
import { useEffect, type ReactNode } from "react";
import { Icon, type IconName } from "../../icons";
import { t } from "../../i18n";

/** How long a line stays: a failure has to be read, and it does not come back. */
const TOAST_MS = 6000;
const UNDO_MS = 5000;

/** A notice is a card rather than a line: a title, what happened, an icon. The notification pop-ups
    are drawn this way, and an error that names its setting. */
export type ToastNotice = { title: string; icon?: IconName; tone?: "attention" | "live" };

type ToastData = { undo?: () => void; action?: { label: string; run: () => void }; notice?: ToastNotice; seq: number };

const manager = Toast.createToastManager<ToastData>();
let hosts = 0;
let seq = 0;
/** The toast on screen, by the number `toast` handed out for it. Only one is ever shown. */
let current: { seq: number; id: string; text: string; timer: number } | null = null;

export type ToastOptions = { undo?: () => void; action?: { label: string; run: () => void }; ms?: number; notice?: ToastNotice };

/**
 * A line for what would otherwise go unnoticed: a failure or a refusal. With `undo`, a button that
 * calls it before the timer runs out, and with `action`, a button of its own words (a browser that
 * opened offers "View"). Never use it to say that something worked: the thing itself says so, and a
 * line of that kind stood over a phone's composer after every press. A press takes it away.
 *
 * One at a time: a new line replaces the one on screen rather than stacking under it, because a
 * stack of failures read as a log the operator had to clear.
 */
export function toast(text: string, opts: ToastOptions = {}): number {
  if (hosts === 0) return 0;
  if (current) closeCurrent();
  const mine = ++seq;
  // The line keeps its own time. Base UI's timer stops while the window is out of focus, and a
  // headless or a background window never has it: the first failure stayed up for good, over the
  // controls under it.
  const id = manager.add({ title: text, timeout: 0, type: opts.notice ? "notice" : "line", data: { undo: opts.undo, action: opts.action, notice: opts.notice, seq: mine } });
  current = { seq: mine, id, text, timer: window.setTimeout(() => dismissToast(mine), opts.ms ?? TOAST_MS) };
  return mine;
}

/** Takes one toast away early, if it is still the one on screen: its offer was taken another way. */
export function dismissToast(handle: number): void {
  if (current && current.seq === handle) closeCurrent();
}

function closeCurrent() {
  if (!current) return;
  window.clearTimeout(current.timer);
  manager.close(current.id);
  current = null;
}

export function ToastHost() {
  useEffect(() => {
    hosts += 1;
    return () => { hosts -= 1; };
  }, []);
  return (
    <Toast.Provider toastManager={manager} limit={1}>
      <Toast.Portal>
        {/* Base UI names the region in English; the app names it in the reader's language. */}
        <Toast.Viewport className="toast-viewport" aria-label={t("dlg.toasts")}>
          <Lines />
        </Toast.Viewport>
      </Toast.Portal>
    </Toast.Provider>
  );
}

function Lines() {
  const { toasts } = Toast.useToastManager<ToastData>();
  // The line being replaced is not drawn through its exit: two `.toast`s at once, for a moment, were
  // two answers to "what does the toast say".
  const shown = toasts.filter((each) => each.transitionStatus !== "ending" && !each.limited).slice(0, 1);
  return <>{shown.map((each) => <Line key={each.id} id={each.id} title={each.title} data={each.data} toastObject={each} />)}</>;
}

function Line({ id, title, data, toastObject }: { id: string; title: ReactNode; data?: ToastData; toastObject: Parameters<typeof Toast.Root>[0]["toast"] }) {
  const done = () => { if (current?.id === id) closeCurrent(); else manager.close(id); };
  const notice = data?.notice;
  return (
    <Toast.Root toast={toastObject} className={`toast ${notice ? `notice ${notice.tone ?? ""}` : ""}`.trim()} role="status" aria-live="polite" swipeDirection={["up", "right"]}
      onClick={(e) => { if (!(e.target as HTMLElement).closest("button")) done(); }}>
      {notice?.icon && <Icon name={notice.icon} size={16} />}
      {notice
        ? <span className="toast-text"><Toast.Title className="toast-title">{notice.title}</Toast.Title><Toast.Description className="toast-body">{title}</Toast.Description></span>
        : <span>{title}</span>}
      {data?.undo && (
        <button type="button" className="btn small ghost" onClick={() => { data.undo?.(); done(); }}>
          {t("common.undo")}
        </button>
      )}
      {data?.action && (
        <button type="button" className="btn small ghost toast-action" onClick={() => { data.action?.run(); done(); }}>
          {data.action.label}
        </button>
      )}
    </Toast.Root>
  );
}

/**
 * Deletes after a pause: the row disappears at once, the toast offers Undo for exactly as long as
 * the request is held back, and the request goes out only when the pause ends without one.
 */
export function deleteWithUndo(label: string, commit: () => Promise<void>, onUndo: () => void, onFail: (e: unknown) => void): void {
  const timer = window.setTimeout(() => {
    if (current?.text === label) closeCurrent();
    commit().catch((e) => {
      onUndo();
      onFail(e);
    });
  }, UNDO_MS);
  toast(label, {
    ms: UNDO_MS,
    undo: () => {
      window.clearTimeout(timer);
      onUndo();
    },
  });
}
