// The composer's text field: plain text that draws a fenced block as the code card an answer draws,
// so a message looks the same typed and sent.
//
// It is an editable element rather than a textarea with a coloured copy drawn under it (the notes
// editor's way). A copy under a textarea must match the textarea letter for letter in one font, so
// the code could not be monospaced, could not scroll sideways inside its card, and the card could not
// have a head: the block would still read as prose with colours. What a textarea gave for free is
// kept on purpose here:
// - typing, spelling fixes and input methods stay the browser's: the browser edits the line, and the
//   text is read back from the page afterwards, whatever kind of input it was. Nothing is redrawn
//   while an input method is composing (a Russian phone keyboard composes every word), only after.
// - only the lines whose drawing changed are replaced, so a line of prose being typed is never
//   rewritten under the caret; a code card is redrawn as it is coloured, and the caret put back.
// - Undo is the field's own (codetext.ts says why the browser's cannot be), for the keys and for the
//   browser's own Undo command alike.
// - Enter, a pasted text and a dropped one are inserted by the field as plain text, never as markup.

import { forwardRef, useCallback, useEffect, useImperativeHandle, useLayoutEffect, useRef } from "react";
import { type CardParts, type Drawn, History, codeEnter, enterAt, inCode, renderBlocks, type Snapshot } from "./codetext";
import { copyButton } from "./md";

/** What the composer asks of its field: the part of a textarea it used. */
export type CodeFieldHandle = {
  readonly element: HTMLDivElement | null;
  readonly value: string;
  readonly selectionStart: number;
  readonly selectionEnd: number;
  focus: (options?: FocusOptions) => void;
  blur: () => void;
  setSelectionRange: (start: number, end: number) => void;
};

type Props = {
  value: string;
  onChange: (text: string) => void;
  placeholder?: string;
  ariaLabel?: string;
  className?: string;
  hidden?: boolean;
  disabled?: boolean;
  /** Called before the field's own keys; a press it prevents is not handled again here. */
  onKeyDown?: (event: React.KeyboardEvent<HTMLDivElement>) => void;
  /** Called first; a paste it prevents (files) is not inserted as text. */
  onPaste?: (event: React.ClipboardEvent<HTMLDivElement>) => void;
  /** The selection moved, by a key, a click or an edit: Quick Chat follows the caret for its `@` and `$`. */
  onSelect?: () => void;
  suggestion?: string;
};

const isLine = (el: Element, root: Element) =>
  el !== root && (el.hasAttribute("data-line") || ((el.tagName === "DIV" || el.tagName === "P") && !el.hasAttribute("data-card")));

/** The text on screen and where the selection stands in it. */
export function readField(root: HTMLElement): Snapshot {
  const sel = typeof window !== "undefined" ? window.getSelection() : null;
  const inside = !!sel && sel.rangeCount > 0 && root.contains(sel.anchorNode) && root.contains(sel.focusNode);
  const range = inside ? sel!.getRangeAt(0) : null;
  let text = "";
  let started = false;
  let start = -1;
  let end = -1;
  const mark = (node: Node, offset: number) => {
    if (!range) return;
    if (node === range.startContainer && offset === range.startOffset && start < 0) start = text.length;
    if (node === range.endContainer && offset === range.endOffset && end < 0) end = text.length;
  };
  const walk = (node: Node) => {
    if (node.nodeType === Node.TEXT_NODE) {
      const data = (node as Text).data;
      started = true;
      if (range && node === range.startContainer && start < 0) start = text.length + Math.min(range.startOffset, data.length);
      if (range && node === range.endContainer && end < 0) end = text.length + Math.min(range.endOffset, data.length);
      text += data;
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const el = node as Element;
    if (el.hasAttribute("data-skip") || el.tagName === "BR") {
      // A line's last <br> only holds an empty line open; any other is a line break the browser put in.
      mark(el.parentNode!, Array.prototype.indexOf.call(el.parentNode!.childNodes, el));
      if (el.tagName === "BR" && el.nextSibling) text += "\n";
      return;
    }
    if (isLine(el, root)) {
      if (started) text += "\n";
      started = true;
    }
    const kids = el.childNodes;
    for (let i = 0; i < kids.length; i++) {
      mark(el, i);
      walk(kids[i]);
    }
    mark(el, kids.length);
  };
  walk(root);
  if (start < 0) start = text.length;
  if (end < 0) end = start;
  return { text, start: Math.min(start, end), end: Math.max(start, end) };
}

/** The node and offset of `offset` in a field just drawn, where every line is a line element. */
function pointAt(root: HTMLElement, offset: number): [Node, number] {
  const lines = root.querySelectorAll("[data-line]");
  let at = 0;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const length = line.textContent?.length ?? 0;
    if (offset <= at + length || i === lines.length - 1) {
      let left = Math.min(offset - at, length);
      const texts = document.createTreeWalker(line, NodeFilter.SHOW_TEXT);
      let last: Text | null = null;
      for (let node = texts.nextNode() as Text | null; node; node = texts.nextNode() as Text | null) {
        if (left <= node.data.length) return [node, left];
        left -= node.data.length;
        last = node;
      }
      return last ? [last, last.data.length] : [line, 0];
    }
    at += length + 1;
  }
  return [root, 0];
}

function select(root: HTMLElement, start: number, end: number) {
  const sel = window.getSelection();
  if (!sel) return;
  const range = document.createRange();
  const [a, ao] = pointAt(root, start);
  range.setStart(a, ao);
  if (end !== start) {
    const [b, bo] = pointAt(root, end);
    range.setEnd(b, bo);
  } else range.collapse(true);
  sel.removeAllRanges();
  sel.addRange(range);
}

/** Bring the caret into view after a change the field made itself; the browser does it for typing. */
function reveal(root: HTMLElement) {
  const sel = window.getSelection();
  if (!sel || !sel.rangeCount) return;
  const rects = sel.getRangeAt(0).getClientRects();
  const rect = rects[rects.length - 1] ?? (sel.focusNode instanceof Element ? sel.focusNode.getBoundingClientRect() : null);
  if (!rect) return;
  const box = root.getBoundingClientRect();
  if (rect.bottom > box.bottom) root.scrollTop += rect.bottom - box.bottom + 4;
  else if (rect.top < box.top) root.scrollTop -= box.top - rect.top + 4;
}

function fromHtml(html: string): Element {
  const holder = document.createElement("template");
  holder.innerHTML = html;
  return holder.content.firstElementChild!;
}

/**
 * Bring `parent`'s children, drawn as `before`, to `after` (the html of each), touching only the run
 * between the common head and the common tail: typing changes one line, Enter adds one. A line whose
 * drawing changed is kept when the browser's own edit already left it exactly so, which is the usual
 * case for a letter typed in prose. Says whether anything was replaced.
 */
function patchLines(parent: Element, before: string[], after: string[], each?: (child: Element, was: number, now: number) => boolean | null): boolean {
  let head = 0;
  while (head < before.length && head < after.length && before[head] === after[head]) head++;
  let tail = 0;
  while (tail < before.length - head && tail < after.length - head && before[before.length - 1 - tail] === after[after.length - 1 - tail]) tail++;
  const gone = before.length - head - tail;
  const come = after.length - head - tail;
  if (!gone && !come) return false;
  if (gone === come) {
    let replaced = false;
    for (let i = head; i < head + gone; i++) {
      const child = parent.children[i];
      // A child the caller patched itself: true when it replaced something, null when it could not.
      const done = each ? each(child, i, i) : null;
      if (done !== null) { replaced ||= done; continue; }
      if (child.outerHTML === after[i]) continue;
      parent.replaceChild(fromHtml(after[i]), child);
      replaced = true;
    }
    return replaced;
  }
  const at = parent.children[head + gone] ?? null;
  for (let i = 0; i < gone; i++) parent.children[head].remove();
  for (let i = head; i < head + come; i++) parent.insertBefore(fromHtml(after[i]), at);
  return true;
}

/** A code card redrawn in place: its label, its copy button's text, and the lines that changed. Null
 *  when the card on the page is no longer the one drawn (the browser joined lines inside it). */
function patchCard(card: Element, was: CardParts, now: CardParts): boolean | null {
  const code = card.querySelector(":scope > .cf-code");
  const head = card.querySelector(":scope > .codehead");
  const close = card.querySelector(":scope > .cf-close");
  if (!code || !head || (was.close === null) !== (now.close === null) || !!close !== (was.close !== null) || code.children.length !== was.lines.length || code.childNodes.length !== was.lines.length) return null;
  if (was.lang !== now.lang) card.setAttribute("data-lang", now.lang);
  if (was.copy !== now.copy) card.querySelector(":scope > [data-skip]")?.setAttribute("data-copy-text", now.copy);
  let replaced = false;
  if (was.head !== now.head && head.outerHTML !== now.head) {
    card.replaceChild(fromHtml(now.head), head);
    replaced = true;
  }
  if (close && now.close !== null && was.close !== now.close && close.outerHTML !== now.close) {
    card.replaceChild(fromHtml(now.close), close);
    replaced = true;
  }
  return patchLines(code, was.lines, now.lines) || replaced;
}

/** The field's children brought from `before` to `after`; a card edited inside is patched line by line. */
function patch(root: Element, before: Drawn[], after: Drawn[]): boolean {
  return patchLines(root, before.map((d) => d.html), after.map((d) => d.html), (child, was, now) => {
    const a = before[was].card;
    const b = after[now].card;
    if (!a || !b || !child.classList.contains("cf-card")) return null;
    const done = patchCard(child, a, b);
    if (done !== null) return done;
    // Not the card it was drawn as: drawn again whole.
    root.replaceChild(fromHtml(after[now].html), child);
    return true;
  });
}

/** The placeholder as an attribute, as a textarea carries it: a div's props do not name it. */
function placeholderAttribute(words?: string): Record<string, string> {
  return words ? { placeholder: words } : {};
}

export const CodeField = forwardRef<CodeFieldHandle, Props>(function CodeField(props, ref) {
  const root = useRef<HTMLDivElement>(null);
  const text = useRef(props.value);
  const painted = useRef<Drawn[]>([]);
  const history = useRef(new History({ text: props.value, start: props.value.length, end: props.value.length }));
  const composing = useRef(false);
  const onChange = useRef(props.onChange);
  onChange.current = props.onChange;

  /** Draw `next`, replacing only what changed, and put the selection back when something was
   *  replaced (or always, when `force`). */
  const paint = useCallback((next: string, caret: [number, number] | null, force = false) => {
    const el = root.current;
    if (!el) return;
    const drawn = renderBlocks(next, copyButton());
    // The page as drawn last time, unless the browser's own edit changed how many lines it holds (a
    // line joined to the one before, a selection across lines deleted): then all of it is redrawn.
    const replaced = el.childNodes.length === painted.current.length && el.children.length === painted.current.length
      ? patch(el, painted.current, drawn)
      : (el.innerHTML = drawn.map((d) => d.html).join(""), true);
    painted.current = drawn;
    if (next) el.removeAttribute("data-empty");
    else el.setAttribute("data-empty", "");
    if (caret && (replaced || force) && document.activeElement === el) select(el, caret[0], caret[1]);
  }, []);

  /** A change the field made itself: drawn, recorded for Undo and handed to the owner. */
  const apply = useCallback((next: Snapshot, record = true) => {
    text.current = next.text;
    paint(next.text, [next.start, next.end], true);
    if (record) history.current.record(next);
    if (root.current) reveal(root.current);
    onChange.current(next.text);
  }, [paint]);

  /** The browser changed the page: read the text back, colour it, tell the owner. */
  const sync = useCallback(() => {
    const el = root.current;
    if (!el || composing.current) return;
    const now = readField(el);
    paint(now.text, [now.start, now.end]);
    if (now.text === text.current) return;
    text.current = now.text;
    history.current.record(now);
    onChange.current(now.text);
  }, [paint]);

  // The owner's value: drawn when it is not what the field already holds (sent and cleared, a command
  // picked from the palette, a voice note landed), with the caret at the end as a textarea puts it.
  useLayoutEffect(() => {
    if (props.value === text.current && painted.current.length) return;
    text.current = props.value;
    const end = props.value.length;
    paint(props.value, [end, end], true);
    if (props.value) history.current.record({ text: props.value, start: end, end });
    else history.current.reset({ text: "", start: 0, end: 0 });
  }, [props.value, paint]);

  // The browser's own commands arrive as beforeinput: Undo from a menu or a phone's shake, a new
  // line from a phone keyboard's return key, a paste or drop not caught as such. React's synthetic
  // beforeinput is a different event, so this listens on the element itself.
  useEffect(() => {
    const el = root.current;
    if (!el) return;
    const onBefore = (event: InputEvent) => {
      const kind = event.inputType;
      if (kind === "historyUndo" || kind === "historyRedo") {
        event.preventDefault();
        const back = kind === "historyUndo" ? history.current.undo() : history.current.redo();
        if (back) apply(back, false);
      } else if ((kind === "insertParagraph" || kind === "insertLineBreak") && !event.isComposing) {
        event.preventDefault();
        const now = readField(el);
        const next = enterAt(now.text, now.start, now.end);
        apply({ text: next.text, start: next.caret, end: next.caret });
      } else if ((kind === "insertFromPaste" || kind === "insertFromDrop") && event.dataTransfer) {
        const words = event.dataTransfer.getData("text/plain");
        if (!words) return;
        event.preventDefault();
        insert(words);
      } else if ((kind === "insertText" || kind === "insertReplacementText") && !event.isComposing) {
        // Text of several lines arriving as one insertion (a correction, an automation filling the
        // field) is put in by the field: the browser would break its lines into markup of its own.
        const words = event.data ?? event.dataTransfer?.getData("text/plain") ?? "";
        if (!words.includes("\n")) return;
        event.preventDefault();
        insert(words);
      }
    };
    const insert = (words: string) => {
      const now = readField(el);
      const clean = words.replace(/\r\n?/g, "\n");
      const next = now.text.slice(0, now.start) + clean + now.text.slice(now.end);
      const caret = now.start + clean.length;
      apply({ text: next, start: caret, end: caret });
    };
    el.addEventListener("beforeinput", onBefore);
    return () => el.removeEventListener("beforeinput", onBefore);
  }, [apply]);

  useImperativeHandle(ref, () => ({
    get element() { return root.current; },
    get value() { return text.current; },
    get selectionStart() { return root.current ? readField(root.current).start : 0; },
    get selectionEnd() { return root.current ? readField(root.current).end : 0; },
    focus: (options?: FocusOptions) => {
      const el = root.current;
      if (!el) return;
      const had = document.activeElement === el;
      el.focus(options);
      // A focused element with no selection in it takes typing at its start; a textarea keeps its
      // caret, which after a programmatic focus is at the end of the text.
      if (!had) {
        const sel = window.getSelection();
        if (!sel || !sel.rangeCount || !el.contains(sel.anchorNode)) select(el, text.current.length, text.current.length);
      }
    },
    blur: () => root.current?.blur(),
    setSelectionRange: (start: number, end: number) => { if (root.current) select(root.current, start, end); },
  }), []);

  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.nativeEvent.isComposing || event.keyCode === 229) return;
    const el = root.current!;
    const plainEnter = event.key === "Enter" && !event.shiftKey && !event.ctrlKey && !event.metaKey && !event.altKey;
    // Inside code, Enter is the code's next line; the message is sent with the modifier, or from outside.
    if (plainEnter && codeEnter(text.current, readField(el).start)) {
      event.preventDefault();
      const now = readField(el);
      const next = enterAt(now.text, now.start, now.end);
      apply({ text: next.text, start: next.caret, end: next.caret });
      return;
    }
    props.onKeyDown?.(event);
    if (event.defaultPrevented) return;
    const mod = event.ctrlKey || event.metaKey;
    const key = event.key.toLowerCase();
    if (mod && !event.altKey && (key === "z" || key === "y" || key === "я" || key === "н")) {
      event.preventDefault();
      const redo = key === "y" || key === "н" || event.shiftKey;
      const back = redo ? history.current.redo() : history.current.undo();
      if (back) apply(back, false);
      return;
    }
    if (event.key === "Enter" && !mod) {
      event.preventDefault();
      const now = readField(el);
      const next = enterAt(now.text, now.start, now.end);
      apply({ text: next.text, start: next.caret, end: next.caret });
      return;
    }
    // Below a card that ends the text there is no line to go down to: one is opened, so the caret
    // can always leave the code.
    if (event.key === "ArrowDown" && !event.shiftKey && !mod) {
      const now = readField(el);
      const lastLine = now.text.lastIndexOf("\n") + 1;
      const last = now.text.slice(lastLine);
      if (now.start >= lastLine && (/^\s*```\s*$/.test(last) || inCode(now.text, now.start))) {
        event.preventDefault();
        const end = now.text.length + 1;
        apply({ text: `${now.text}\n`, start: end, end });
      }
    }
  };

  const onPaste = (event: React.ClipboardEvent<HTMLDivElement>) => {
    props.onPaste?.(event);
    if (event.defaultPrevented) return;
    const words = event.clipboardData?.getData("text/plain") ?? "";
    event.preventDefault();
    if (!words) return;
    const now = readField(root.current!);
    const clean = words.replace(/\r\n?/g, "\n");
    const caret = now.start + clean.length;
    apply({ text: now.text.slice(0, now.start) + clean + now.text.slice(now.end), start: caret, end: caret });
  };

  return (
    <div
      ref={root}
      className={`composer-field ${props.className ?? ""}`}
      contentEditable={props.disabled ? false : "plaintext-only"}
      suppressContentEditableWarning
      role="textbox"
      aria-multiline="true"
      aria-label={props.ariaLabel}
      aria-placeholder={props.placeholder}
      aria-disabled={props.disabled || undefined}
      {...placeholderAttribute(props.placeholder)}
      data-placeholder={props.placeholder}
      data-suggestion={props.suggestion || undefined}
      hidden={props.hidden}
      tabIndex={props.disabled ? -1 : 0}
      spellCheck
      onInput={sync}
      onCompositionStart={() => { composing.current = true; }}
      onCompositionEnd={() => { composing.current = false; sync(); }}
      onKeyDown={onKeyDown}
      onPaste={onPaste}
      onSelect={props.onSelect}
      onMouseDown={(event) => {
        // The copy button keeps the caret where it was; the page's handler (md.ts) copies.
        if ((event.target as Element).closest("[data-skip]")) event.preventDefault();
      }}
    />
  );
});
