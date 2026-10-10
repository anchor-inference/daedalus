// Which keys a focused terminal keeps for the program in it, and which the app takes.
//
// A terminal wants nearly every key: Ctrl+K kills a line in the shell, Ctrl+\ quits, Ctrl+R searches
// history. So while a terminal has focus, the app's own shortcuts step aside (`insideTerminal`), and
// only this short list is taken back. Keys are matched by `code`, the physical key, not by `key`:
// on a Russian layout Ctrl+` produces "ё" and Ctrl+Shift+C produces "С", and the shortcut has to work
// whatever layout is active.

export type TerminalAction =
  | "toggle-dock"
  | "copy"
  | "copy-clear"
  | "paste"
  | "search"
  | "font-bigger"
  | "font-smaller"
  | "font-reset"
  | "previous-mark"
  | "next-mark";

/** The fields of a `KeyboardEvent` this reads. */
export type KeyLike = Pick<KeyboardEvent, "code" | "ctrlKey" | "shiftKey" | "altKey" | "metaKey">;

export type KeyContext = {
  /** macOS, where Cmd is the copy/paste modifier and Ctrl belongs to the terminal. */
  mac: boolean;
  /** The alternate screen is up (vim, htop, less): Ctrl+↑/↓ go to the program, not to the marks. */
  altScreen: boolean;
  /** xterm.js holds a selection of its own (a plain drag, or Shift/Option-drag over a mouse program). */
  selection?: boolean;
  /** The program reports the mouse (Claude Code's full-screen view, vim with `mouse=a`): a plain
   *  drag is the program's selection, drawn by the program, and only the program can copy it. */
  mouseProgram?: boolean;
  /** The program asked for a copy (OSC 52) the browser refused for want of a key press: the next
   *  copy key delivers it. */
  held?: boolean;
};

/**
 * The app's action for a key pressed in a terminal, or null when the key belongs to the program.
 * Wired into `attachCustomKeyEventHandler`, which is told `false` (do not send) whenever this is not null.
 */
export function reservedKey(e: KeyLike, context: KeyContext): TerminalAction | null {
  if (e.altKey) return null;
  if (context.mac && e.metaKey && !e.ctrlKey) {
    if (e.code === "KeyC") return copyKey(context);
    if (e.code === "KeyV") return "paste";
    return null;
  }
  if (!e.ctrlKey || e.metaKey) return null;
  if (e.code === "Backquote" && !e.shiftKey) return "toggle-dock";
  // Plain Ctrl+C copies while something is selected, as in VS Code and Windows Terminal, and is the
  // interrupt otherwise. Not on a Mac: there Cmd copies and Ctrl+C is always the interrupt.
  if (!context.mac && !e.shiftKey && e.code === "KeyC" && context.selection) return "copy-clear";
  if (e.shiftKey && e.code === "KeyC") return copyKey(context);
  if (e.shiftKey && e.code === "KeyV") return "paste";
  if (e.shiftKey && e.code === "KeyF") return "search";
  if (e.code === "Equal" || e.code === "NumpadAdd") return "font-bigger";
  if (e.code === "Minus" || e.code === "NumpadSubtract") return "font-smaller";
  if ((e.code === "Digit0" || e.code === "Numpad0") && !e.shiftKey) return "font-reset";
  if (!context.altScreen && !e.shiftKey) {
    if (e.code === "ArrowUp") return "previous-mark";
    if (e.code === "ArrowDown") return "next-mark";
  }
  return null;
}

/**
 * Cmd+C or Ctrl+Shift+C. With nothing selected in xterm.js and a program that reports the mouse,
 * the key goes to the program: Claude Code's full-screen view binds both to copying its own
 * selection, which it then hands back through OSC 52. Taking the key there (as this once did
 * always) copied nothing and left the program never knowing it was asked. Over a plain shell the
 * key stays taken even with nothing selected: Ctrl+Shift+C would otherwise reach the shell as an
 * interrupt in the legacy key encoding, and open the browser's inspector on the way. A held copy
 * from the program wins over passing the key on: the program has already answered.
 */
function copyKey(context: KeyContext): TerminalAction | null {
  return !context.selection && !context.held && context.mouseProgram ? null : "copy";
}

/** Whether a key event's target is inside a terminal, where the app's global shortcuts stand aside. */
export function insideTerminal(target: EventTarget | null): boolean {
  const element = target as { closest?: (selector: string) => unknown } | null;
  return !!element && typeof element.closest === "function" && !!element.closest(".xterm, [data-terminal]");
}

/** Whether the platform is macOS (the copy/paste modifier is Cmd there). */
export function isMac(): boolean {
  const platform = (navigator as { userAgentData?: { platform?: string } }).userAgentData?.platform ?? navigator.platform ?? "";
  return /mac/i.test(platform);
}
