// What the operator's keyboard and mouse become on the page while they drive.
//
// Printable text does not go as key presses: it goes as `text` (the daemon's `Input.insertText`), from
// the viewer's hidden field, which is the one place a browser hands over composed text — an IME's
// finished word, a phone keyboard's autocorrection, dictation, a paste. What is sent as a key is what
// has no text: Enter, Tab, Backspace, the arrows, Escape, and anything held with Ctrl, Alt or Meta, so
// Ctrl+A selects on the page and Ctrl+Enter submits a form.
//
// A few chords are the viewer's rather than the page's, the ones a person uses on a browser without
// thinking: the address bar, reload, back and forward. They would otherwise go to the app's own tab.

import type { InputMessage, Mods } from "./protocol";

/** CDP's modifier bits, which the daemon passes on as they are. */
export const MOD = { alt: 1, ctrl: 2, meta: 4, shift: 8 } as const;

type KeyLike = Pick<KeyboardEvent, "key" | "code" | "keyCode" | "altKey" | "ctrlKey" | "metaKey" | "shiftKey">;
type MouseLike = Pick<MouseEvent, "altKey" | "ctrlKey" | "metaKey" | "shiftKey">;

export function modsOf(e: MouseLike): Mods {
  return (e.altKey ? MOD.alt : 0) | (e.ctrlKey ? MOD.ctrl : 0) | (e.metaKey ? MOD.meta : 0) | (e.shiftKey ? MOD.shift : 0);
}

/** Keys the page gets as presses. A letter alone is text; the same letter with Ctrl is a key. */
const NAMED = new Set([
  "Enter", "Tab", "Backspace", "Delete", "Escape", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
  "Home", "End", "PageUp", "PageDown", "Insert",
  "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12",
]);

/** Windows virtual key codes for the named keys, which CDP wants beside the key's name. */
const KEY_CODES: Record<string, number> = {
  Backspace: 8, Tab: 9, Enter: 13, Escape: 27, " ": 32, PageUp: 33, PageDown: 34, End: 35, Home: 36,
  ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39, ArrowDown: 40, Insert: 45, Delete: 46,
};

/** Whether a key press goes to the page as a key (true) or is left to arrive as text (false). */
export function isKeyPress(e: KeyLike): boolean {
  if (e.key === "Dead" || e.key === "Process" || e.key === "Unidentified") return false;
  if (["Shift", "Control", "Alt", "Meta", "CapsLock", "AltGraph"].includes(e.key)) return false;
  if (NAMED.has(e.key)) return true;
  // A character with Ctrl or Meta is a shortcut on the page (Ctrl+A); with Alt alone it is often a
  // character on its own (Option+e on a Mac, AltGr on a European layout), so it stays text.
  return (e.ctrlKey || e.metaKey) && e.key.length === 1;
}

function keyCode(e: KeyLike): number {
  if (KEY_CODES[e.key] !== undefined) return KEY_CODES[e.key];
  if (/^F([1-9]|1[0-2])$/.test(e.key)) return 111 + Number(e.key.slice(1));
  if (e.key.length === 1) {
    const upper = e.key.toUpperCase();
    if (/[A-Z0-9]/.test(upper)) return upper.charCodeAt(0);
  }
  return e.keyCode || 0;
}

/** The INPUT for one key going down or up. `text` rides with Enter, which is how a page sees a newline typed. */
export function keyInput(e: KeyLike, type: "down" | "up"): Extract<InputMessage, { t: "key" }> {
  const text = type === "down" && e.key === "Enter" && !e.ctrlKey && !e.metaKey && !e.altKey ? "\r" : undefined;
  return { t: "key", type, key: e.key, code: e.code, key_code: keyCode(e), ...(text ? { text } : {}), mods: modsOf(e) };
}

/** A named key pressed and released, from the phone's row of keys. */
export function tapKey(key: string): Extract<InputMessage, { t: "key" }>[] {
  const code = key.startsWith("Arrow") || NAMED.has(key) ? key : "";
  const e = { key, code, keyCode: 0, altKey: false, ctrlKey: false, metaKey: false, shiftKey: false };
  return [keyInput(e, "down"), keyInput(e, "up")];
}

export function mouseButton(button: number): "left" | "middle" | "right" {
  return button === 1 ? "middle" : button === 2 ? "right" : "left";
}

/** The viewer's own chords, caught before the page or the app's tab gets them. */
export type ViewerChord = "address" | "reload" | "back" | "forward" | "swallow";

export function viewerChord(e: KeyLike, mac: boolean): ViewerChord | null {
  const primary = mac ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey;
  if (primary && !e.altKey) {
    const k = e.key.toLowerCase();
    if (k === "l") return "address";
    if (k === "r") return "reload";
    if (e.key === "[") return "back";
    if (e.key === "]") return "forward";
    // New tab and close tab would act on the app's own window; the viewer has no tabs of its own to
    // open or close for a person yet, so the chord is simply kept from leaving.
    if (k === "t" || k === "w") return "swallow";
  }
  if (!mac && e.altKey && !e.ctrlKey && !e.metaKey) {
    if (e.key === "ArrowLeft") return "back";
    if (e.key === "ArrowRight") return "forward";
  }
  if (e.key === "F5" && !e.altKey) return "reload";
  return null;
}

/**
 * The clipboard's chords, which cross between the operator's machine and the page. A paste is never
 * sent as keys: the page's browser would paste its own clipboard, which holds nothing of the
 * operator's. It is left to the viewer's hidden field, whose `paste` event carries the operator's
 * clipboard and goes to the page as text. A copy or a cut is sent as keys, so the page's own copy
 * handlers and a web terminal's Ctrl+C still see them, and also asks the daemon for the selection,
 * which the viewer puts on the operator's clipboard.
 */
export type ClipboardChord = "paste" | "copy" | "cut";

export function clipboardChord(e: KeyLike, mac: boolean): ClipboardChord | null {
  if (e.altKey) return null;
  const primary = mac ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey;
  // The letter by its key where the layout gives one, by its place where it does not: Ctrl+V on a
  // Russian layout arrives as "м" with code KeyV.
  const letter = e.key.length === 1 && /[a-z]/i.test(e.key) ? e.key.toLowerCase() : e.code === "KeyV" ? "v" : e.code === "KeyC" ? "c" : e.code === "KeyX" ? "x" : "";
  if (primary) {
    if (letter === "v") return "paste";
    if (letter === "c" && !e.shiftKey) return "copy";
    if (letter === "x" && !e.shiftKey) return "cut";
  }
  // The older chords a Linux or Windows keyboard still has.
  if (!mac && e.key === "Insert" && !e.metaKey) {
    if (e.shiftKey && !e.ctrlKey) return "paste";
    if (e.ctrlKey && !e.shiftKey) return "copy";
  }
  return null;
}

/**
 * Escape belongs to the page first — a menu, a modal, a search field all close with it — so the viewer
 * lets go of the keyboard only on a second press within this long.
 */
export const ESCAPE_TWICE_MS = 600;
