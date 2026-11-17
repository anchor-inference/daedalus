// Small UI helpers shared by the screens: Telegram bridge, confirmations, formatting.

import { ApiError, telegram } from "./api";
import { confirmDialog } from "./ui/dialogs";
import { asSentence, bytes, tokens } from "./format";
import { t } from "./i18n";

/** The Telegram bridge only when the app really runs inside Telegram: the script also loads in a plain
 *  browser, where it reports version 6.0 and rejects every method (showConfirm → WebAppMethodUnsupported). */
function insideTelegram() {
  const tg = telegram();
  return tg && tg.initData ? tg : null;
}

/** Ask before a destructive action: the app's own dialog, which says what the action does. */
export function confirmAsync(text: string, opts: { body?: string; action?: string; danger?: boolean } = {}): Promise<boolean> {
  return confirmDialog({ title: text, body: opts.body, action: opts.action ?? t("common.confirm"), danger: opts.danger ?? true });
}

/** Whether Enter should send: desktop clients send, phones insert a newline. */
export function enterSends(): boolean {
  const platform = insideTelegram()?.platform;
  if (platform && platform !== "unknown") return !["ios", "android", "android_x"].includes(platform);
  return !("ontouchstart" in window) || window.matchMedia?.("(pointer: fine)").matches;
}

export function haptic(kind: "light" | "medium" | "success" | "error" = "light"): void {
  const h = insideTelegram()?.HapticFeedback;
  if (!h) return;
  try {
    if (kind === "success" || kind === "error") h.notificationOccurred(kind);
    else h.impactOccurred(kind);
  } catch {
    /* an old client without haptics */
  }
}

export const fmtBytes = bytes;
export const fmtTok = tokens;

/** A number field's value, or null when it is empty or not a number (then nothing is saved). */
export function numInput(raw: string, min?: number): number | null {
  if (raw.trim() === "") return null;
  const v = Number(raw);
  if (Number.isNaN(v) || !Number.isFinite(v)) return null;
  if (min !== undefined && v < min) return null;
  return v;
}

/**
 * A message from the host shown as a sentence of its own. The host writes its reasons in lower case
 * so they read inside a longer line ("could not start: the model refused: …"); standing alone under
 * a form, the same lower-case text read as a fragment next to the page's own capitalised copy.
 */
export function sentence(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

// The statuses a request can end in with no reason from the host, each said in the operator's words.
const STATUS_TEXT: Record<number, string> = {
  400: "common.status.400", 401: "common.status.401", 403: "common.status.403", 404: "common.status.404",
  405: "common.status.405", 409: "common.status.409", 422: "common.status.422", 429: "common.status.429",
  500: "common.status.500", 502: "common.status.502", 503: "common.status.503", 504: "common.status.504",
};

/**
 * A readable message for a failed request. ``failed`` says what was being done ("Could not run the
 * schedule"), and the reason follows it. The reason is the host's own when it gave one; when it gave
 * only a status, HTTP's stock phrase ("Not Found") is replaced by what that status means here, in the
 * operator's language — that phrase alone was once the whole toast, and said neither what failed nor why.
 */
export function errorText(e: unknown, failed?: string): string {
  const reason = errorReason(e);
  if (!failed) return reason;
  // Inside the line the reason reads as its continuation; an acronym ("MCP …") keeps its capitals.
  const why = /^\p{Lu}\p{Ll}/u.test(reason) ? reason.charAt(0).toLowerCase() + reason.slice(1) : reason;
  return `${failed}: ${why}`;
}

function errorReason(e: unknown): string {
  if (e instanceof ApiError && e.status === 413) return t("upload.toolarge");
  if (e instanceof ApiError && e.bare) {
    const key = STATUS_TEXT[e.status];
    return key ? t(key) : t(e.status >= 500 ? "common.status.server" : "common.status.other", { status: e.status });
  }
  const msg = e instanceof Error ? e.message : String(e);
  if (!msg || msg === "Failed to fetch" || msg === "Load failed" || msg === "NetworkError when attempting to fetch resource.") return t("common.noconnection");
  return asSentence(msg);
}
