// A keyboard shortcut as the desktop's global shortcut takes it (Electron's accelerator), recorded
// from a key press on the settings page, and shown back the way the operator's system writes it.
//
// The key is read from `code`, the physical key, not `key`: on a Russian layout the key labelled K
// types «л», and a shortcut recorded as Alt+Л is one Electron cannot register at all.

export type KeyPress = { code: string; altKey: boolean; ctrlKey: boolean; metaKey: boolean; shiftKey: boolean };

const NAMED: Record<string, string> = {
  Space: "Space", Enter: "Return", NumpadEnter: "Return", Tab: "Tab", Backspace: "Backspace", Delete: "Delete", Insert: "Insert",
  Home: "Home", End: "End", PageUp: "PageUp", PageDown: "PageDown", ArrowUp: "Up", ArrowDown: "Down", ArrowLeft: "Left", ArrowRight: "Right",
  Minus: "-", Equal: "=", BracketLeft: "[", BracketRight: "]", Backslash: "\\", Semicolon: ";", Quote: "'", Comma: ",", Period: ".", Slash: "/", Backquote: "`",
  PrintScreen: "PrintScreen",
};

function keyName(code: string): string | null {
  if (/^Key[A-Z]$/.test(code)) return code.slice(3);
  if (/^Digit[0-9]$/.test(code)) return code.slice(5);
  if (/^Numpad[0-9]$/.test(code)) return `num${code.slice(6)}`;
  if (/^F([1-9]|1[0-9]|2[0-4])$/.test(code)) return code;
  return NAMED[code] ?? null;
}

/** The accelerator a press makes, or null while only modifiers are held or the key cannot be one.
 *  A plain key without a modifier is refused (it would be taken from every other program), except
 *  a function key. */
export function acceleratorFrom(press: KeyPress, mac: boolean): string | null {
  const key = keyName(press.code);
  if (!key) return null;
  const parts: string[] = [];
  if (mac) {
    if (press.metaKey) parts.push("Command");
    if (press.ctrlKey) parts.push("Control");
  } else if (press.ctrlKey) parts.push("Ctrl");
  if (press.altKey) parts.push("Alt");
  if (press.shiftKey) parts.push("Shift");
  if (!mac && press.metaKey) parts.push("Super");
  if (!parts.length && !/^F\d+$/.test(key)) return null;
  return [...parts, key].join("+");
}

const MAC_SIGNS: Record<string, string> = { command: "⌘", cmd: "⌘", control: "⌃", ctrl: "⌃", alt: "⌥", option: "⌥", shift: "⇧", commandorcontrol: "⌘", cmdorctrl: "⌘" };

/** How a Mac writes it (⌥ Space) and how everyone else does (Alt+Space). */
export function showAccelerator(accelerator: string, mac: boolean): string {
  if (!accelerator) return "";
  const parts = accelerator.split("+");
  if (!mac) return parts.map((part) => (part.toLowerCase() === "commandorcontrol" || part.toLowerCase() === "cmdorctrl" ? "Ctrl" : part)).join("+");
  const key = parts[parts.length - 1];
  return [...parts.slice(0, -1).map((part) => MAC_SIGNS[part.toLowerCase()] ?? part), key].join(" ");
}
