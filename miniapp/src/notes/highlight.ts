// The editor's source, tinted: the marks of Markdown dimmed, headings and file references brought
// forward, code in the code colour. It is drawn under a textarea whose own letters are transparent, so
// the caret, the selection, input methods and the phone's keyboard stay the browser's.
//
// The tint changes colours only, never a letter's width: every character of the source is in the
// drawing exactly once, in order, in the same font, size and weight. A bold heading in the drawing
// would push every following letter away from the caret the textarea draws over it.

/** Past this the source is drawn plainly: the tint is rebuilt on every keystroke. */
export const TINT_MAX = 60_000;

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function span(cls: string, text: string): string {
  return text ? `<span class="${cls}">${esc(text)}</span>` : "";
}

/** One line's inline marks: code, references and links, emphasis. */
function inline(line: string): string {
  let out = "";
  let at = 0;
  const re = /(`[^`\n]+`)|(!?\[(?:\\.|[^\]\\\n])*\]\((?:<[^>\n]*>|[^)\s]*)\))|(\*\*[^*\n]+\*\*|__[^_\n]+__)|(~~[^~\n]+~~)|((?<![\w*])\*(?!\s)[^*\n]+?\*(?![\w*])|(?<![\w_])_(?!\s)[^_\n]+?_(?![\w_]))/g;
  for (const m of line.matchAll(re)) {
    out += esc(line.slice(at, m.index));
    const whole = m[0];
    if (m[1]) out += span("hl-code", whole);
    else if (m[2]) {
      const close = whole.lastIndexOf("](");
      const target = whole.slice(close + 2, -1);
      const label = whole.slice(whole.startsWith("!") ? 2 : 1, close);
      const kind = /^<?files\//.test(target) ? "hl-ref" : "hl-url";
      out += span("hl-mark", whole.startsWith("!") ? "![" : "[") + span("hl-label", label) + span("hl-mark", "](") + span(kind, target) + span("hl-mark", ")");
    } else if (m[3]) out += span("hl-mark", whole.slice(0, 2)) + span("hl-strong", whole.slice(2, -2)) + span("hl-mark", whole.slice(-2));
    else if (m[4]) out += span("hl-mark", "~~") + span("hl-strike", whole.slice(2, -2)) + span("hl-mark", "~~");
    else out += span("hl-mark", whole[0]) + span("hl-em", whole.slice(1, -1)) + span("hl-mark", whole.slice(-1));
    at = (m.index ?? 0) + whole.length;
  }
  return out + esc(line.slice(at));
}

/** The source as tinted markup; its text content is the source itself, letter for letter. */
export function tint(source: string): string {
  const lines = source.split("\n");
  let fence = false;
  const out = lines.map((line) => {
    if (/^\s*```/.test(line)) {
      fence = !fence;
      return span("hl-fence", line);
    }
    if (fence) return span("hl-code", line);
    if (/^\s*:::/.test(line)) return span("hl-fence", line);
    const heading = /^(#{1,6}\s+)(.*)$/.exec(line);
    if (heading) return span("hl-mark", heading[1]) + `<span class="hl-h">${inline(heading[2])}</span>`;
    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) return span("hl-mark", line);
    const quote = /^(\s*(?:>\s?)+)(.*)$/.exec(line);
    if (quote) return span("hl-mark", quote[1]) + `<span class="hl-quote">${inline(quote[2])}</span>`;
    const item = /^(\s*(?:[-*+]|\d+[.)])\s+)(\[[ xX]\]\s)?(.*)$/.exec(line);
    if (item) {
      const done = !!item[2] && /x/i.test(item[2]);
      return span("hl-mark", item[1]) + (item[2] ? span(done ? "hl-box done" : "hl-box", item[2]) : "") + (done ? `<span class="hl-done">${inline(item[3])}</span>` : inline(item[3]));
    }
    if (/^\s*\|/.test(line)) return line.split(/(\|)/).map((part) => (part === "|" ? span("hl-mark", part) : /^\s*:?-+:?\s*$/.test(part) ? span("hl-mark", part) : inline(part))).join("");
    return inline(line);
  });
  // A trailing newline in a textarea opens a line the drawing must open too, or the last line of the
  // two would sit a line apart.
  return out.join("\n") + "\n";
}
