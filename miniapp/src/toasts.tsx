// Notifications as they arrive: a stack of at most three in the bottom-right corner of a desktop,
// one banner at the top of a phone (the bottom of a phone holds the tab bar and the composer).
//
// Only what the host marked `toast` is shown, only when it happened now (a replay after a reconnect
// updates the lists but never pops up), never while the page is hidden, never about the session or
// terminal this window already shows (the conversation itself says it), and never on a device where
// the operator switched pop-ups off (`popups.ts`). A pop-up is a glance, not a record: every one of
// them is already in the inbox behind the bell, so letting one go, or all of them, loses nothing.
//
// This module decides what becomes a pop-up and what it says; the queue, the stack, the hold, the
// swipe and the foot line are the primitives' toast host (`ui/primitives/notices.tsx`), the same one
// the status line at the bottom is drawn by (`dialogs.tsx`).

import { useEffect, useRef } from "react";
import type { AppEvent, Notification } from "./api";
import { toast as statusLine } from "./ui/dialogs";
import { NoticeGlyph, clearNotices, dismissNotice, raiseNotice, setNoticeFoot, type NoticeView } from "./ui/primitives/notices";
import { type EventMeta, useEvent } from "./events";
import { setPopupsShown, usePopupsShown } from "./popups";
import { shownScopes } from "./presence";
import { navigate, pathFor } from "./router";
import { ActionButtons, categoryLabel, markSeen, noticeIcon, openEntry, toneClass } from "./notifications";
import { t } from "./i18n";
import { usePetPreference } from "./ui/pet";
import { goToSetting, NOTICE_EVENT, SettingButton, type SettingNotice, settingPath } from "./settinglink";

/** A notification that only reports something: long enough to read two lines twice. */
export const TOAST_MS = 7000;
/** Something went wrong or is urgent but asks nothing: a little longer, so a glance away does not miss it. */
export const URGENT_MS = 12000;
/** A request with answers on it: long enough to answer from the pop-up, but it still leaves, because
 *  the request waits in the inbox under "Needs you" for as long as it is open. */
export const ACTIONABLE_MS = 20000;
/** How long a pop-up about this entry stays: a request with answers on it longest, a failure or a way
 *  to a setting a little longer than a line that only reports. */
export function toastDuration(entry: Notification): number {
  if (entry.needs_you && entry.actions.some((a) => a.id !== "open")) return ACTIONABLE_MS;
  // A way to a setting is something to press, not only to read: the longer stay, warning or error.
  if (entry.level === "urgent" || entry.tone === "error" || entry.setting) return URGENT_MS;
  return TOAST_MS;
}

export type ToastContext = { visible: boolean; shown: { sessions: Set<string>; terminals: Set<string>; projects: Set<string> } };

/** Whether a `notify` event becomes a toast in this window. */
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

/** What a pop-up about an entry draws: its glyph in the tone's colour, the title and text, the way to
 *  a setting it names, and the buttons the host put on it. */
/** Which setting and tone an entry speaks for, or undefined when it speaks for none. */
export function settingGroup(entry: Pick<Notification, "setting" | "tone">): string | undefined {
  return entry.setting ? `${entry.setting.key}:${entry.tone}` : undefined;
}

function viewOf(entry: Notification): NoticeView {
  const leave = () => { dismissNotice(entry.id); };
  return {
    title: entry.title,
    body: entry.body || undefined,
    icon: <NoticeGlyph name={noticeIcon(entry)} className={toneClass(entry)} label={categoryLabel(entry.category)} />,
    urgent: entry.level === "urgent",
    tone: entry.setting ? entry.tone : undefined,
    group: settingGroup(entry),
    onOpen: () => {
      openEntry(entry);
      leave();
    },
    actions: (
      <>
        {entry.setting && (
          <div className="notice-actions">
            <SettingButton setting={entry.setting} className="btn primary" onGo={() => { if (!entry.seen && entry.id > 0) void markSeen([entry.id]); leave(); }} />
          </div>
        )}
        <ActionButtons entry={entry} onDone={leave} />
      </>
    ),
  };
}

function raise(entry: Notification, seq: number) {
  const view = viewOf(entry);
  // One pop-up per setting and tone: a failed /compact is told by its own answer and by the host's
  // notification about the same summary model, and two pop-ups saying it was one too many.
  raiseNotice({ key: entry.id, seq, ms: toastDuration(entry), view, replaces: view.group ? (other) => other.group === view.group : undefined });
}

/** Wires the host's notifications, the app's own setting notices and the answers that arrive from other
 *  windows to the pop-up host; draws nothing itself. */
export function NotificationToasts() {
  const [pet] = usePetPreference();
  const enabled = usePopupsShown();
  const raised = useRef<number[]>([]);

  // Switched off in another window, or in Settings while some were up: they go at once.
  useEffect(() => {
    if (!enabled) clearNotices();
  }, [enabled]);
  useEffect(() => {
    if (pet) clearNotices();
  }, [pet]);

  useEffect(() => {
    const openInbox = () => {
      clearNotices();
      navigate(pathFor("inbox"));
    };
    const turnOff = () => {
      setPopupsShown(false);
      clearNotices();
      statusLine(t("notice.popups.off"), { undo: () => setPopupsShown(true), ms: 8000 });
    };
    setNoticeFoot({ onOpenWaiting: openInbox, onTurnOff: turnOff, onCloseAll: () => { clearNotices(); } });
    return () => setNoticeFoot(null);
  }, []);

  useEvent(["notify"], (event, meta) => {
    if (!enabled || !shouldToast(event.payload, meta, currentContext())) return;
    const seq = event.seq || Date.now();
    if (event.seq) {
      if (raised.current.includes(event.seq)) return;
      raised.current = [...raised.current.slice(-RAISED_MAX + 1), event.seq];
    }
    if (pet) {
      window.dispatchEvent(new CustomEvent("daedalus:pet-notice", { detail: event.payload.notification as Notification }));
      return;
    }
    raise(event.payload.notification as Notification, seq);
  });
  // A notice the app raised itself — an error that names its setting — in the same corner, with the
  // same button. With pop-ups off (or the companion speaking for them) the status line says it, with
  // the button as its action: an error the operator must act on is never dropped for a preference.
  useEffect(() => {
    const show = (e: Event) => {
      const notice = (e as CustomEvent<SettingNotice>).detail;
      if (!enabled || pet) {
        statusLine(`${notice.title}: ${notice.body}`, { action: { label: t("setting.go"), run: () => goToSetting(notice.setting) }, ms: 10000 });
        return;
      }
      raise(localEntry(notice), Date.now());
    };
    window.addEventListener(NOTICE_EVENT, show);
    return () => window.removeEventListener(NOTICE_EVENT, show);
  }, [enabled, pet]);
  // Answered or read somewhere else: the pop-up has nothing left to say.
  useEvent(["notify.resolved"], (event) => {
    if (pet) window.dispatchEvent(new CustomEvent("daedalus:pet-clear", { detail: Number(event.payload.id) }));
    dismissNotice(Number(event.payload.id));
  });
  useEvent(["notify.seen"], (event) => {
    const ids = event.payload.ids;
    if (pet) window.dispatchEvent(new CustomEvent("daedalus:pet-clear", { detail: ids }));
    if (ids === "all") clearNotices();
    else if (Array.isArray(ids)) for (const id of ids) dismissNotice(Number(id));
  });
  return null;
}
/** A notice the app raised, shaped as a notification so the toast draws it like every other one. Its
 *  id is negative: no entry of the host's has one, so it is never marked seen or answered there. */
export function localEntry(notice: SettingNotice): Notification {
  const now = new Date().toISOString();
  return {
    id: -notice.id, at: now, updated_at: now, category: "system", kind: "setting", level: "normal", tone: notice.tone,
    title: notice.title, body: notice.body, link: settingPath(notice.setting), session_id: null, run_id: null, project_id: null,
    staff_id: null, terminal_id: null, source: "app", dedupe_key: null, request_ref: null, count: 1, actions: [], seen: true,
    resolved: null, needs_you: false, delivered: {}, setting: notice.setting,
  };
}
