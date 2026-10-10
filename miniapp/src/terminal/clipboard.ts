// Writing the clipboard from a terminal: by key, by menu, and for a program that asks (OSC 52).

/**
 * Copies text; true when it reached the clipboard.
 *
 * The page is usually served over plain http on a LAN address, which is not a secure context:
 * `navigator.clipboard` does not exist there at all. So the first way is the copy command, which
 * works on any origin during a key press or a click: it fires a `copy` event, and the listener
 * puts the text on the event's own clipboard. The focus stays where it was — the hidden text field
 * this used to select took it from the terminal, so the keys typed after a copy went nowhere.
 * WebKit enables the command only when the `beforecopy` event is cancelled or something is
 * selected, so that event is cancelled too. Outside a key press (OSC 52 from a program arrives
 * with output) only the asynchronous clipboard can work, and only in a secure context.
 */
export async function copyText(text: string): Promise<boolean> {
  if (copyNow(text)) return true;
  try {
    if (!navigator.clipboard) return false;
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/** The synchronous half of `copyText`: the copy command, with a selected text field as the last resort. */
export function copyNow(text: string): boolean {
  let written = false;
  const enable = (e: Event) => e.preventDefault();
  const fill = (e: ClipboardEvent) => {
    if (!e.clipboardData) return;
    e.clipboardData.setData("text/plain", text);
    written = true;
    e.preventDefault();
    // xterm.js listens for `copy` too and would put its own (empty) selection over this.
    e.stopImmediatePropagation();
  };
  document.addEventListener("beforecopy", enable, true);
  document.addEventListener("copy", fill, true);
  try {
    if (document.execCommand("copy") && written) return true;
  } catch {
    // Refused outright: the text field below fares no better, but costs nothing to try.
  } finally {
    document.removeEventListener("beforecopy", enable, true);
    document.removeEventListener("copy", fill, true);
  }
  return copyThroughField(text);
}

/** Copies by selecting a hidden text field, for a webview that fires no `copy` event without a selection; the focus is given back. */
function copyThroughField(text: string): boolean {
  const focused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const field = document.createElement("textarea");
  field.value = text;
  field.setAttribute("readonly", "");
  field.style.position = "fixed";
  field.style.opacity = "0";
  document.body.appendChild(field);
  field.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  field.remove();
  focused?.focus({ preventScroll: true });
  return ok;
}
