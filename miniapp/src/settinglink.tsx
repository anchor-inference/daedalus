// The way from a problem to the setting that fixes it.
//
// A refusal about one setting — the summary model over its limit, no vision model set, speech
// recognition not chosen — used to end in a sentence naming a page, and the operator went looking for
// the card and the row. The host now names the row: a `setting` of `{page, key}` beside the `detail`
// of an HTTP error, in a `setting_notice` session event, and in a notification (read from its link).
// `page` is the Settings section; `key` is the configuration path the row edits, and the row carries
// it as `data-setting`. This module turns that reference into an address, finds the row once the page
// has drawn it, scrolls it into view and makes it blink; and it raises a notice — a toast in the
// corner, and a line in the conversation it came from — with a button that goes there.

import { useEffect, useState } from "react";
import { Icon } from "./icons";
import { ApiError } from "./api";
import { t } from "./i18n";
import { navigate, pathFor } from "./router";

export type SettingRef = { page: string; key: string };

/** How long a row found by a link stays highlighted: two blinks, long enough to be seen after the scroll. */
export const FLASH_MS = 2400;
export const FLASH_CLASS = "setting-flash";
/** The query parameter a settings address names its row with. */
export const SETTING_PARAM = "setting";

/** A `{page, key}` from anything the host sent, or null when it is not one. */
export function settingRef(value: unknown): SettingRef | null {
  if (!value || typeof value !== "object") return null;
  const { page, key } = value as Record<string, unknown>;
  return typeof page === "string" && page && typeof key === "string" && key ? { page, key } : null;
}

/** The setting an error is about: the `setting` the host put beside its `detail`. */
export function settingOfError(error: unknown): SettingRef | null {
  return error instanceof ApiError ? settingRef(error.data.setting) : null;
}

/** The address that opens the section and names the row. The models page keeps its endpoints on a
 *  tab of their own, and a provider's key is only on the page while that tab is open. */
export function settingPath(ref: SettingRef): string {
  const tab = ref.page === "models" && ref.key.startsWith("providers.") ? "providers" : null;
  return pathFor("settings", ref.page, { [SETTING_PARAM]: ref.key, tab });
}

export function goToSetting(ref: SettingRef): void {
  navigate(settingPath(ref));
}

/** The row's selector. A key is a configuration path, which holds dots and may hold a provider's
 *  own id, so it is quoted rather than trusted to be a plain identifier. */
export function rowSelector(key: string): string {
  return `[data-setting="${key.replace(/["\\]/g, "\\$&")}"]`;
}

/**
 * Find the row, scroll it into the middle of its scroller and make it blink.
 *
 * Settings are read from the host after the page opens, and a section draws its rows only then, so
 * the row is looked for a few times rather than once; the answer says whether it was found. The class
 * is taken off again after `FLASH_MS`, and put on afresh when it is already there, so a second press
 * of the same button blinks again instead of doing nothing.
 */
export function revealSetting(key: string, opts: { root?: ParentNode; tries?: number; every?: number; flashMs?: number } = {}): Promise<boolean> {
  const root = opts.root ?? document;
  const tries = opts.tries ?? 30;
  const every = opts.every ?? 100;
  return new Promise((resolve) => {
    let left = tries;
    const attempt = () => {
      const row = root.querySelector<HTMLElement>(rowSelector(key));
      if (!row) {
        if (--left <= 0) resolve(false);
        else window.setTimeout(attempt, every);
        return;
      }
      row.classList.remove(FLASH_CLASS);
      void row.offsetWidth; // restart the animation when the row was already blinking
      row.classList.add(FLASH_CLASS);
      const reduced = typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      row.scrollIntoView?.({ block: "center", behavior: reduced ? "auto" : "smooth" });
      window.setTimeout(() => row.classList.remove(FLASH_CLASS), opts.flashMs ?? FLASH_MS);
      resolve(true);
    };
    attempt();
  });
}

// ── notices ────────────────────────────────────────────────────────────────────────────────

/** `warning`: the session's model stood in and the work was done. `error`: nothing did it. */
export type SettingNotice = {
  id: number;
  tone: "warning" | "error";
  title: string;
  body: string;
  setting: SettingRef;
  /** The conversation it came from, which draws it inline; null for one raised by a screen. */
  sessionId: string | null;
  /** Whether the corner toast shows it too. */
  toast: boolean;
};

export const NOTICE_EVENT = "daedalus:setting-notice";
const KEPT = 20;
let seq = 0;
let notices: SettingNotice[] = [];
const listeners = new Set<() => void>();

/** Raise a notice: kept for the conversation's inline line, and announced for the corner toast. */
export function raiseSettingNotice(notice: Omit<SettingNotice, "id" | "toast"> & { toast?: boolean }): SettingNotice {
  // A repeat of the same notice for the same place replaces it: a summary model that refuses at
  // every turn is one line, not a column of them.
  const raised: SettingNotice = { ...notice, toast: notice.toast ?? true, id: ++seq };
  notices = [...notices.filter((n) => !(n.sessionId === raised.sessionId && n.setting.key === raised.setting.key && n.tone === raised.tone)), raised].slice(-KEPT);
  listeners.forEach((l) => l());
  if (raised.toast) window.dispatchEvent(new CustomEvent<SettingNotice>(NOTICE_EVENT, { detail: raised }));
  return raised;
}

export function dismissSettingNotice(id: number): void {
  notices = notices.filter((n) => n.id !== id);
  listeners.forEach((l) => l());
}

export function settingNotices(sessionId: string | null): SettingNotice[] {
  return notices.filter((n) => n.sessionId === sessionId);
}

/** The notices one conversation draws inline. */
export function useSettingNotices(sessionId: string | null): SettingNotice[] {
  const [list, setList] = useState(() => settingNotices(sessionId));
  useEffect(() => {
    const update = () => setList(settingNotices(sessionId));
    update();
    listeners.add(update);
    return () => {
      listeners.delete(update);
    };
  }, [sessionId]);
  return list;
}

/** A `setting_notice` event of a session's stream as a notice: the words are the app's, by kind. */
export function noticeFromEvent(sessionId: string, payload: Record<string, unknown>): Omit<SettingNotice, "id" | "toast"> | null {
  const setting = settingRef(payload.setting);
  if (!setting) return null;
  const outcome = payload.outcome === "fallback" ? "fallback" : "failed";
  const kind = typeof payload.kind === "string" ? payload.kind : "";
  const known = ["compaction", "vision", "search"].includes(kind);
  const model = typeof payload.model === "string" && payload.model ? payload.model : "";
  const detail = typeof payload.detail === "string" ? payload.detail : "";
  return {
    tone: outcome === "fallback" ? "warning" : "error",
    title: t(known ? `setting.notice.${kind}.${outcome}` : `setting.notice.other.${outcome}`),
    body: outcome === "fallback" && model ? `${detail} → ${model}` : detail,
    setting,
    sessionId,
  };
}

/**
 * The corner toast for an error that names its setting, with the button; false when it names none,
 * so the caller says it the way it always did. `sessionId` puts the same line in that conversation.
 */
export function raiseFromError(error: unknown, sessionId: string | null = null): boolean {
  const setting = settingOfError(error);
  if (!setting) return false;
  // Said once however many hands it passes through: the screen that knows the conversation raises
  // it first, and the composer that catches the same error after it only learns it was said.
  if (raisedErrors.has(error as object)) return true;
  raisedErrors.add(error as object);
  raiseSettingNotice({ tone: "error", title: t("setting.notice.refused"), body: (error as Error).message, setting, sessionId });
  return true;
}

const raisedErrors = new WeakSet<object>();

export function SettingButton({ setting, onGo, className = "btn small" }: { setting: SettingRef; onGo?: () => void; className?: string }) {
  return (
    <button
      type="button"
      className={`${className} setting-go`}
      data-setting-go={setting.key}
      onClick={(e) => {
        e.stopPropagation();
        goToSetting(setting);
        onGo?.();
      }}
    >
      {t("setting.go")}
    </button>
  );
}

/** The conversation's own notices, drawn above its composer: what the session's model stood in for,
 *  or what could not be done, each with the way to its setting. They stay until dismissed, because
 *  the toast in the corner leaves on its own and the operator may come back to the chat later. */
export function SettingNoticeLines({ sessionId }: { sessionId: string }) {
  const list = useSettingNotices(sessionId);
  if (list.length === 0) return null;
  return (
    <div className="setting-notices">
      {list.map((n) => (
        <div key={n.id} className={`setting-notice tone-${n.tone}`} role={n.tone === "error" ? "alert" : "status"} data-setting-notice={n.setting.key}>
          <Icon name="alert" size={14} />
          <span className="setting-notice-text"><b>{n.title}</b>{n.body ? <span className="sub"> {n.body}</span> : null}</span>
          <SettingButton setting={n.setting} />
          <button type="button" className="iconbtn small quiet" aria-label={t("notice.dismiss")} title={t("notice.dismiss")} onClick={() => dismissSettingNotice(n.id)}>
            <Icon name="close" size={14} />
          </button>
        </div>
      ))}
    </div>
  );
}
