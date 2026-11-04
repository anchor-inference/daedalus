// The editor's text field is a plain textarea: paste, spell checking, input methods and a phone's own
// keyboard all stay the browser's. Two things a textarea does not give are done here: a command that
// rewrites the text keeps the browser's own Undo, and the caret's place on screen is measured for the
// menus that open beside it.

import type { Edit } from "./model";

/**
 * Put an edit into the field as if typed. Only the part that changed is replaced, through the
 * browser's own insertion, so Ctrl+Z takes the command back in one step; setting the value instead
 * cleared the field's whole undo history with the first bold. Where the browser offers no insertion
 * command, the value is set and `fallback` tells the owner of the state.
 */
export function applyEdit(field: HTMLTextAreaElement, edit: Edit, fallback: (text: string) => void): void {
  const before = field.value;
  const after = edit.text;
  let head = 0;
  while (head < before.length && head < after.length && before[head] === after[head]) head++;
  let tail = 0;
  while (tail < before.length - head && tail < after.length - head && before[before.length - 1 - tail] === after[after.length - 1 - tail]) tail++;
  field.focus();
  field.setSelectionRange(head, before.length - tail);
  const middle = after.slice(head, after.length - tail);
  let done = false;
  try {
    done = middle ? document.execCommand("insertText", false, middle) : document.execCommand("delete", false);
  } catch {
    done = false;
  }
  if (!done || field.value !== after) {
    field.value = after;
    fallback(after);
  }
  field.setSelectionRange(edit.start, edit.end);
}

const COPIED = ["boxSizing", "width", "fontFamily", "fontSize", "fontWeight", "fontStyle", "letterSpacing", "lineHeight", "paddingTop", "paddingRight", "paddingBottom", "paddingLeft", "borderTopWidth", "borderRightWidth", "borderBottomWidth", "borderLeftWidth", "tabSize", "textIndent", "wordSpacing"] as const;

/** Where the character at `index` is drawn, relative to the field's own box (its scroll included): a
 *  hidden copy of the field's text up to that point, laid out the same way, is measured. */
export function caretPoint(field: HTMLTextAreaElement, index: number): { top: number; left: number; height: number } {
  const style = getComputedStyle(field);
  const mirror = document.createElement("div");
  for (const key of COPIED) mirror.style[key] = style[key];
  mirror.style.position = "absolute";
  mirror.style.visibility = "hidden";
  mirror.style.whiteSpace = "pre-wrap";
  mirror.style.overflowWrap = "break-word";
  mirror.style.top = "0";
  mirror.style.left = "-9999px";
  mirror.textContent = field.value.slice(0, index);
  const mark = document.createElement("span");
  mark.textContent = "​";
  mirror.appendChild(mark);
  document.body.appendChild(mirror);
  const lineHeight = parseFloat(style.lineHeight) || parseFloat(style.fontSize) * 1.5;
  const point = { top: mark.offsetTop - field.scrollTop, left: mark.offsetLeft - field.scrollLeft, height: lineHeight };
  mirror.remove();
  return point;
}
