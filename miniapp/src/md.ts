// Markdown → HTML for the chat: headings, paragraphs, lists (nested, ordered, tasks), tables,
// fenced code with a language label and a copy button, quotes, dividers, inline marks, links,
// <details>/<summary> passthrough, and the evidence tags an answer cites (<file …/>, <run …/>) as
// clickable chips. Everything else is HTML-escaped first, so model output cannot inject markup.

import { t } from "./i18n";

function escape(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/** `<file path="a.py" lines="3-9"/>` and `<run id="v12" label="14 passed"/>` — what an answer cites. */
const EVIDENCE_RE = /<\s*(file|run)\s+([^<>]*?)\/?\s*>/g;
const ATTR_RE = /([a-z_]+)\s*=\s*"([^"]*)"/g;

function attrs(raw: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const m of raw.matchAll(ATTR_RE)) out[m[1]] = m[2].trim();
  return out;
}

/** One evidence tag as a chip; the click is handled once for the whole app (see the bottom of this file). */
function evidenceChip(kind: string, raw: string): string | null {
  const a = attrs(raw);
  if (kind === "file") {
    if (!a.path) return null;
    const lines = a.lines ? `<span class="chip-lines">:${escape(a.lines)}</span>` : "";
    const where = a.path + (a.lines ? `:${a.lines}` : "");
    return `<button type="button" class="evidence file" data-evidence="file" data-path="${escape(a.path)}"${a.lines ? ` data-lines="${escape(a.lines)}"` : ""} title="${escape(where)}"><span aria-hidden>📄</span><span class="chip-text">${escape(a.path)}</span>${lines}</button>`;
  }
  if (!a.id) return null;
  return `<button type="button" class="evidence run" data-evidence="run" data-run="${escape(a.id)}" title="${escape(t("md.run", { id: a.id }))}"><span aria-hidden>🧾</span><span class="chip-text">${escape(a.label || a.id)}</span></button>`;
}

// The stash marks a slot in the text with a character the reader's own markdown cannot
// carry: a private-use code point, not the NUL this used to use. A NUL makes the file
// binary to git, and a source file nobody can read a diff of is its own defect.
const STASH = "\uE000";

function inline(s: string): string {
  const codes: string[] = [];
  // The sentinel is a private-use character nothing legitimately writes; text that carries it
  // could otherwise address a stashed element by number, so it is dropped before stashing.
  s = s.split(STASH).join("");
  s = s.replace(/`([^`\n]+)`/g, (_, c) => {
    codes.push(`<code>${escape(c)}</code>`);
    return ` ${STASH}${codes.length - 1}${STASH} `;
  });
  // After the code stash, so a tag quoted in backticks stays quoted, and before the escape, so the
  // tag itself never reaches the reader as markup.
  s = s.replace(EVIDENCE_RE, (whole, kind: string, raw: string) => {
    const chip = evidenceChip(kind, raw);
    if (chip === null) return whole;
    codes.push(chip);
    return ` ${STASH}${codes.length - 1}${STASH} `;
  });
  s = escape(s);
  s = s.replace(/&lt;(\/?)(details|summary)&gt;/g, "<$1$2>");
  s = s.replace(/&lt;details open&gt;/g, "<details open>");
  s = s.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
  s = s.replace(/__(.+?)__/g, "<b>$1</b>");
  s = s.replace(/(^|[^\w*])\*(?!\s)([^*\n]*?[^\s*])\*(?![\w*])/g, "$1<i>$2</i>");
  s = s.replace(/(^|[^\w_])_(?!\s)([^_\n]*?[^\s_])_(?![\w_])/g, "$1<i>$2</i>");
  s = s.replace(/~~(.+?)~~/g, "<s>$1</s>");
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
  // Only known app routes are accepted as relative links; arbitrary relative paths could
  // turn model text into a request to an authenticated API endpoint.
  s = s.replace(/\[([^\]]+)\]\((\/app\/diagrams\/[a-f0-9]{32})\)/g, '<a href="$2">$1</a>');
  s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noreferrer">$2</a>');
  return s.replace(/ \uE000(\d+)\uE000 /g, (_, i) => codes[Number(i)]);
}

type ListItem = { text: string; children: string[]; task?: boolean; checked?: boolean };

function indentOf(line: string): number {
  return (line.match(/^(\s*)/) as RegExpMatchArray)[1].length;
}

function renderList(lines: string[]): string {
  const ordered = /^\s*\d+[.)]\s/.test(lines[0]);
  const base = indentOf(lines[0]);
  const items: ListItem[] = [];
  for (const line of lines) {
    const indent = indentOf(line);
    const body = line.replace(/^\s*(?:[-*+]|\d+[.)])\s+/, "");
    if (indent > base && items.length) items[items.length - 1].children.push(line);
    else {
      const task = /^\[( |x|X)\]\s/.test(body);
      items.push({ text: task ? body.slice(4) : body, children: [], task, checked: task && /^\[(x|X)\]/.test(body) });
    }
  }
  const tag = ordered ? "ol" : "ul";
  const html = items
    .map((it) => {
      const check = it.task ? `<span class="check${it.checked ? " on" : ""}">${it.checked ? "✓" : ""}</span>` : "";
      const kids = it.children.length ? renderList(it.children) : "";
      return `<li${it.task ? ' class="task"' : ""}>${check}${inline(it.text)}${kids}</li>`;
    })
    .join("");
  return `<${tag}>${html}</${tag}>`;
}

function renderTable(lines: string[]): string {
  const rows = lines.map((l) => l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim()));
  if (rows.length < 2) return `<p>${inline(lines.join("\n")).replace(/\n/g, "<br/>")}</p>`;
  const align = rows[1].map((c) => (/^:-+:$/.test(c) ? "center" : /-+:$/.test(c) ? "right" : "left"));
  const head = `<tr>${rows[0].map((c, i) => `<th style="text-align:${align[i] ?? "left"}">${inline(c)}</th>`).join("")}</tr>`;
  const body = rows
    .slice(2)
    .map((r) => `<tr>${r.map((c, i) => `<td style="text-align:${align[i] ?? "left"}">${inline(c)}</td>`).join("")}</tr>`)
    .join("");
  return `<div class="tablewrap"><table><thead>${head}</thead><tbody>${body}</tbody></table></div>`;
}

// The copy control is a glyph, not the word: it sits in the head of every block and the word was
// the loudest thing in it. Two drawings, and the button's state says which one shows.
const COPY_GLYPH = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 9h11v11H9zM15 9V4H4v11h5"/></svg>';
const DONE_GLYPH = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12l5 5 9-11"/></svg>';

/** A command-line flag held together: a wrapped block may break after any hyphen, and a phone
 *  showed "-resize 1600x -" on one line and "quality 78" on the next, which reads as a broken
 *  flag. The span changes no text, so what the copy button takes is the command as it ran. */
function keepFlags(html: string): string {
  return html.replace(/(^|[\s(])(--?[A-Za-z][\w-]*)/gm, '$1<span class="nobreak">$2</span>');
}

export function codeBlock(code: string, lang = ""): string {
  const label = lang || "text";
  const copy = escape(t("common.copy"));
  return `<div class="codecard"><div class="codehead"><span>${escape(label)}</span><button class="copy" data-copy="1" type="button" aria-label="${copy}" title="${copy}"><span class="glyph-copy">${COPY_GLYPH}</span><span class="glyph-done">${DONE_GLYPH}</span></button></div><pre><code>${keepFlags(escape(code))}</code></pre></div>`;
}

const LIST_RE = /^\s*(?:[-*+]|\d+[.)])\s+/;
const TABLE_SEP_RE = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;

/**
 * The rendering of a message that is settled: keyed by the message's seq and the length of its
 * text, and checked against the text itself, so a turn that scrolls out of the window and back in
 * costs nothing. Only settled messages are put here; streaming text is parsed as it grows.
 */
const rendered = new Map<string, { text: string; html: string }>();
const RENDER_CACHE_MAX = 400;

export function renderCached(key: string, text: string): string {
  const k = `${key}:${text.length}`;
  const hit = rendered.get(k);
  if (hit && hit.text === text) return hit.html;
  const html = renderMarkdown(text);
  rendered.set(k, { text, html });
  if (rendered.size > RENDER_CACHE_MAX) {
    const oldest = rendered.keys().next();
    if (!oldest.done) rendered.delete(oldest.value);
  }
  return html;
}

export function renderMarkdown(text: string): string {
  const out: string[] = [];
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  let i = 0;
  const para: string[] = [];
  const flush = () => {
    if (para.length) {
      out.push(`<p>${inline(para.join("\n")).replace(/\n/g, "<br/>")}</p>`);
      para.length = 0;
    }
  };
  while (i < lines.length) {
    const line = lines[i];
    const fence = line.match(/^\s*```([\w+#.-]*)\s*$/);
    if (fence) {
      flush();
      const buf: string[] = [];
      i++;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) buf.push(lines[i++]);
      i++;
      out.push(codeBlock(buf.join("\n"), fence[1]));
      continue;
    }
    if (/^\s*$/.test(line)) {
      flush();
      i++;
      continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      flush();
      const level = Math.min(heading[1].length + 1, 4);
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      i++;
      continue;
    }
    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      flush();
      out.push("<hr/>");
      i++;
      continue;
    }
    if (/^\s*>/.test(line)) {
      flush();
      const buf: string[] = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ""));
      out.push(`<blockquote>${renderMarkdown(buf.join("\n"))}</blockquote>`);
      continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && lines[i + 1].includes("|") && TABLE_SEP_RE.test(lines[i + 1])) {
      flush();
      const buf: string[] = [];
      while (i < lines.length && lines[i].includes("|") && /^\s*\|/.test(lines[i])) buf.push(lines[i++]);
      out.push(renderTable(buf));
      continue;
    }
    if (LIST_RE.test(line)) {
      flush();
      const buf: string[] = [];
      while (i < lines.length && (LIST_RE.test(lines[i]) || (/^\s{2,}\S/.test(lines[i]) && buf.length))) buf.push(lines[i++]);
      out.push(renderList(buf));
      continue;
    }
    if (/^\s*<\/?(details|summary)/.test(line)) {
      flush();
      out.push(inline(line.trim()));
      i++;
      continue;
    }
    para.push(line);
    i++;
  }
  flush();
  let html = out.join("");
  const opened = (html.match(/<details/g) ?? []).length;
  const closed = (html.match(/<\/details>/g) ?? []).length;
  if (opened > closed) html += "</details>".repeat(opened - closed);
  return html;
}

/** What a click on an evidence chip asks for; the session screen listens and opens the file or the run. */
export type EvidenceRequest = { kind: "file"; path: string; lines?: string } | { kind: "run"; id: string };

export const EVIDENCE_EVENT = "daedalus:evidence";

// Copy buttons and evidence chips inside rendered markdown: one delegated handler for the whole app.
if (typeof document !== "undefined") {
  document.addEventListener("click", (e) => {
    const target = e.target as HTMLElement | null;
    const chip = target?.closest?.("button[data-evidence]") as HTMLButtonElement | null;
    if (chip) {
      const kind = chip.dataset.evidence;
      const detail: EvidenceRequest | null =
        kind === "file" && chip.dataset.path
          ? { kind: "file", path: chip.dataset.path, lines: chip.dataset.lines }
          : kind === "run" && chip.dataset.run
            ? { kind: "run", id: chip.dataset.run }
            : null;
      if (detail) document.dispatchEvent(new CustomEvent<EvidenceRequest>(EVIDENCE_EVENT, { detail }));
      return;
    }
    const button = target?.closest?.("button[data-copy]") as HTMLButtonElement | null;
    if (!button) return;
    // A block whose text is not one <code> (an edit drawn as removed and added halves) carries
    // what to copy on the button itself.
    const code = button.dataset.copyText ?? button.closest(".codecard")?.querySelector("code")?.textContent ?? "";
    navigator.clipboard?.writeText(code).then(
      () => {
        button.dataset.state = "done";
        button.setAttribute("aria-label", t("common.copied"));
        setTimeout(() => {
          delete button.dataset.state;
          button.setAttribute("aria-label", t("common.copy"));
        }, 1200);
      },
      () => undefined,
    );
  });
}
