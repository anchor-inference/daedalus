// The composer's text as the composer draws it: prose lines as they are typed, and a fenced block
// as the card an answer draws (md.ts), coloured by its label. Every character of the text is in the
// drawing exactly once and in order, the fences included, so the text can always be read back from
// what is on screen and a caret's place in one is a place in the other. codefield.tsx owns the
// editing; this file is the part that needs no browser.

import { HIGHLIGHT_MAX_LINES, highlightLines, langOfLabel } from "./highlight";

/** The opening fence of a block, as md.ts reads it: three backticks and an optional label. */
export const FENCE_OPEN = /^(\s*```)([\w+#.-]*)(\s*)$/;
const FENCE_CLOSE = /^\s*```\s*$/;

export type Block =
  | { kind: "prose"; from: number; lines: string[] }
  | { kind: "code"; from: number; open: string; label: string; body: string[]; close: string | null };

/** The text cut into prose and code blocks; `from` is the index of the block's first line. A block
 *  whose fence is not closed yet runs to the end, as one does while it is being typed. */
export function blocks(text: string): Block[] {
  const lines = text.split("\n");
  const out: Block[] = [];
  let prose: string[] = [];
  let proseFrom = 0;
  let i = 0;
  while (i < lines.length) {
    const open = FENCE_OPEN.exec(lines[i]);
    if (!open) {
      if (!prose.length) proseFrom = i;
      prose.push(lines[i++]);
      continue;
    }
    if (prose.length) out.push({ kind: "prose", from: proseFrom, lines: prose });
    prose = [];
    const from = i;
    const body: string[] = [];
    i++;
    while (i < lines.length && !FENCE_CLOSE.test(lines[i])) body.push(lines[i++]);
    const close = i < lines.length ? lines[i++] : null;
    out.push({ kind: "code", from, open: lines[from], label: open[2], body, close });
  }
  if (prose.length) out.push({ kind: "prose", from: proseFrom, lines: prose });
  return out;
}

const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

/** One line of the field. An empty one holds a <br>, or the browser would draw it no height and
 *  give the caret nowhere to stand. */
function line(html: string, cls = ""): string {
  return `<div class="cf-line${cls ? ` ${cls}` : ""}" data-line="">${html || "<br>"}</div>`;
}

/** A code card's parts, each drawn on its own so that typing in one line of code redraws that line. */
export type CardParts = { lang: string; copy: string; head: string; lines: string[]; close: string | null };

/** One top-level child of the field: a line of prose, or a code card with its parts. */
export type Drawn = { html: string; card?: CardParts };

/** The code card's copy button: the field's own, carrying the code itself (the card's text holds the
 *  fences too), and skipped when the text is read back. */
function cardButton(copy: string, code: string): string {
  return copy.replace("<button ", `<button contenteditable="false" data-skip="" data-copy-text="${esc(code)}" `);
}

/** The card's markup around its code lines: everything but the lines, with the code element empty. */
function cardHtml(parts: CardParts, button: string): string {
  return `<div class="codecard cf-card" data-card="" spellcheck="false" data-lang="${esc(parts.lang)}">` +
    button + parts.head +
    `<div class="cf-code" data-card="">${parts.lines.join("")}</div>` +
    (parts.close ?? "") + `</div>`;
}

/**
 * The field's drawing, one item per top-level child, so the field replaces only what changed: the line
 * being typed in plain prose is never rewritten under the caret, and in code only the line typed in.
 * `copy` is the copy button's markup.
 */
export function renderBlocks(text: string, copy: string): Drawn[] {
  const out: Drawn[] = [];
  for (const block of blocks(text)) {
    if (block.kind === "prose") {
      for (const words of block.lines) out.push({ html: line(esc(words)) });
      continue;
    }
    const colours = block.body.length > HIGHLIGHT_MAX_LINES ? "text" : langOfLabel(block.label);
    const code = block.body.length ? highlightLines(block.body.join("\n"), colours) : [];
    const open = FENCE_OPEN.exec(block.open)!;
    const parts: CardParts = {
      lang: block.label || "text",
      copy: block.body.join("\n"),
      head: line(`<span class="cf-tick">${esc(open[1])}</span>${open[2] ? `<span class="cf-lang">${esc(open[2])}</span>` : ""}${esc(open[3])}`, "codehead cf-fence"),
      lines: code.map((html) => line(html)),
      close: block.close === null ? null : line(`<span class="cf-tick">${esc(block.close)}</span>`, "cf-fence cf-close"),
    };
    out.push({ html: cardHtml(parts, cardButton(copy, parts.copy)), card: parts });
  }
  return out;
}

/** Whether the caret at `offset` stands inside a block's code, between its fences: Enter there is a
 *  new line of code, never Send. */
export function inCode(text: string, offset: number): boolean {
  const row = text.slice(0, offset).split("\n").length - 1;
  return blocks(text).some((b) => b.kind === "code" && row > b.from && row <= b.from + b.body.length);
}

/** Whether Enter at `offset` is a new line of code rather than Send: inside a block, or at the end of
 *  its opening fence, where nobody means to send "```python" alone. */
export function codeEnter(text: string, offset: number): boolean {
  const row = text.slice(0, offset).split("\n").length - 1;
  return blocks(text).some((b) => b.kind === "code" && row >= b.from && row <= b.from + b.body.length);
}

/**
 * What Enter does at `offset` besides breaking the line. On an opening fence that nothing closes yet
 * the block is closed at once, with the caret on the empty line inside it, as the chat apps do; inside
 * code the new line keeps the indentation of the one it breaks.
 */
export function enterAt(text: string, start: number, end: number): { text: string; caret: number } {
  const before = text.slice(0, start);
  const after = text.slice(end);
  const lineStart = before.lastIndexOf("\n") + 1;
  const current = before.slice(lineStart);
  const rest = after.split("\n")[0];
  if (FENCE_OPEN.test(current + rest) && !rest.trim()) {
    const row = before.split("\n").length - 1;
    const open = blocks(text).find((b) => b.kind === "code" && b.from === row);
    if (open && open.kind === "code" && open.close === null && open.body.every((l) => !l.trim())) {
      const indent = /^\s*/.exec(current)![0];
      const inserted = `\n${indent}\n${indent}\`\`\``;
      return { text: before + rest + inserted + after.slice(rest.length), caret: before.length + rest.length + 1 + indent.length };
    }
  }
  const indent = inCode(text, start) ? /^[ \t]*/.exec(current)![0] : "";
  return { text: `${before}\n${indent}${after}`, caret: start + 1 + indent.length };
}

/** One state of the field for Undo: the text and where the selection was. */
export type Snapshot = { text: string; start: number; end: number };

/**
 * The field's own Undo. The browser's history cannot serve: it records changes to the page, and the
 * field rewrites its lines as it colours them, after which the browser's Undo either does nothing or
 * puts back markup the text no longer has. Typing runs into one step until a pause, the end of a word,
 * or a jump of the caret, as a text field's own Undo groups it.
 */
export class History {
  private past: Snapshot[] = [];
  private future: Snapshot[] = [];
  private last = 0;
  constructor(private now: Snapshot, private readonly limit = 200, private readonly pause = 800) {}

  get current(): Snapshot {
    return this.now;
  }

  record(next: Snapshot, at = Date.now()): void {
    if (next.text === this.now.text) {
      this.now = next;
      return;
    }
    const typed = next.text.length === this.now.text.length + 1 && next.start === this.now.start + 1 && next.text.startsWith(this.now.text.slice(0, this.now.start));
    // A step ends where a word does: the space or the line break after it starts the next one, so Undo
    // takes back " world" and leaves "hello".
    const space = (c: string | undefined) => c === undefined || /\s/.test(c);
    const wordEnds = space(next.text[next.start - 1]) && !space(this.now.text[this.now.start - 1]);
    const group = typed && !wordEnds && at - this.last < this.pause && this.past.length > 0;
    if (!group) {
      this.past.push(this.now);
      if (this.past.length > this.limit) this.past.shift();
    }
    this.future = [];
    this.now = next;
    this.last = at;
  }

  undo(): Snapshot | null {
    const back = this.past.pop();
    if (!back) return null;
    this.future.push(this.now);
    this.now = back;
    this.last = 0;
    return back;
  }

  redo(): Snapshot | null {
    const forward = this.future.pop();
    if (!forward) return null;
    this.past.push(this.now);
    this.now = forward;
    this.last = 0;
    return forward;
  }

  /** Start again from `text`: a sent message is not something Undo brings back. */
  reset(snapshot: Snapshot): void {
    this.past = [];
    this.future = [];
    this.now = snapshot;
    this.last = 0;
  }
}
