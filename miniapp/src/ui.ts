// Small UI helpers shared by the screens: Telegram bridge, confirmations, formatting.

import { telegram } from "./api";
import { confirmDialog } from "./dialogs";
import { bytes, tokens } from "./format";
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

/** A readable message for a failed request. */
export function errorText(e: unknown): string {
  const msg = e instanceof Error ? e.message : String(e);
  if (!msg || msg === "Failed to fetch" || msg === "Load failed" || msg === "NetworkError when attempting to fetch resource.") return t("common.noconnection");
  return msg;
}
