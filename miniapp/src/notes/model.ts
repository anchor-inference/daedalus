// The Notes tab's model, with nothing of React in it: the shapes the host sends, the reference form a
// note names a file by, what a card shows of a note, the editor's text commands (lists that go on,
// indentation, marks around a selection), drafts kept on this device, the archive an export downloads,
// and the line diff the conflict prompt shows.
//
// A file is named by plain relative Markdown, `![name](files/<id>/<name>)` for a picture and
// `[name](files/<id>/<name>)` for anything else. The id is the identity and the name decoration, so a
// renamed note or file breaks nothing; an exported note unpacked beside its `files/<id>/<name>` folder
// renders the same in any Markdown reader. The host writes the same form (daedalus/stores/notes.py).

import { noteTaskLines } from "../md";
import { plainPreview } from "../format";

export type NoteScope = "chat" | "project";
export type Author = "operator" | "agent";
export type Note = {
  id: string;
  scope: NoteScope;
  scope_id: string;
  title: string;
  body: string;
  color: string;
  pinned: boolean;
  position: number;
  version: number;
  created_at: string;
  updated_at: string;
  updated_by: Author;
  deleted_at?: string | null;
};
export type NoteFile = { id: string; scope: NoteScope; scope_id: string; name: string; mime: string; size: number; width: number | null; height: number | null; created_at: string; created_by: Author };
export type ScopeListing = { notes: Note[]; files: NoteFile[] };
export type NotesListing = { session_id: string; project: { id: string; name: string } | null; chat: ScopeListing; project_scope: ScopeListing | null };

export const COLORS = ["red", "amber", "green", "teal", "blue", "violet"] as const;
export type NoteColor = (typeof COLORS)[number] | "";

export function notesKey(sessionId: string): string {
  return `/api/sessions/${sessionId}/notes`;
}

/** Where a note file's bytes are: the shape every viewer of the app reads (`{base}/download?path=`). */
export function fileBase(id: string): string {
  return `/api/notes/files/${id}`;
}

export function isImage(file: Pick<NoteFile, "mime">): boolean {
  return file.mime.startsWith("image/");
}

// ── references ───────────────────────────────────────────────────────────────────────────────

const REFERENCE_RE = /\]\(\s*<?files\/([a-z0-9]{12})(?=[/)>\s])/g;

/** The ids of the files a body names, in order of first mention. */
export function references(body: string): string[] {
  return [...new Set([...body.matchAll(REFERENCE_RE)].map((m) => m[1]))];
}

/** The Markdown that shows a file. Brackets in the name are escaped for the link text, and anything that
 *  could end or bend the path is percent-encoded; letters of any script stay readable. */
export function reference(file: Pick<NoteFile, "id" | "name" | "mime">): string {
  const text = file.name.replace(/([[\]\\])/g, "\\$1");
  const path = [...file.name].map((ch) => (/[\p{L}\p{N}\-_.~]/u.test(ch) ? ch : [...new TextEncoder().encode(ch)].map((b) => `%${b.toString(16).toUpperCase().padStart(2, "0")}`).join(""))).join("");
  const target = `files/${file.id}/${path}`;
  return isImage(file) ? `![${text}](${target})` : `[${text}](${target})`;
}

/** The files of the scopes a note can see, by id: its own scope's, and a chat note also its project's. */
export function fileIndex(...lists: (NoteFile[] | undefined)[]): Map<string, NoteFile> {
  const map = new Map<string, NoteFile>();
  for (const list of lists) for (const file of list ?? []) map.set(file.id, file);
  return map;
}

// ── what a card shows ────────────────────────────────────────────────────────────────────────

/** A note's words as one plain line: no marks, no boxes, no references, no code. Lines that stand
 *  alone — a heading, a list item, a table row — are kept apart by a dot, so "Voice" and the
 *  sentence under it do not run together into "Voice Warm, plain…"; a sentence wrapped onto the next
 *  line is joined with a space. */
export function excerpt(body: string, max = 180): string {
  const parts: string[] = [];
  let fence = false;
  let block = true;
  for (const raw of body.replace(/\r\n?/g, "\n").split("\n")) {
    if (/^\s*(```|:::)/.test(raw)) {
      fence = /^\s*```/.test(raw) ? !fence : fence;
      block = true;
      continue;
    }
    if (fence || !raw.trim() || /^\s*\|?\s*:?-{2,}/.test(raw) || /^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(raw)) {
      block = true;
      continue;
    }
    const standalone = /^\s*(#{1,6}\s|[-*+]\s|\d+[.)]\s|>|\|)/.test(raw);
    const line = raw.trim().startsWith("|") ? raw.split("|").map((cell) => cell.trim()).filter(Boolean).join(", ") : raw.replace(/^\s*(?:[-*+]|\d+[.)])\s+\[[ xX]\]\s+/, "").replace(/^\s*(?:>\s?)+/, "");
    const words = plainPreview(line, 400);
    if (!words) continue;
    if (parts.length && !block && !standalone) parts[parts.length - 1] += ` ${words}`;
    else parts.push(words);
    block = standalone;
  }
  return plainPreview(parts.join(" · "), max);
}

/** The first picture a note shows, for the card's thumbnail. */
export function firstImage(body: string, files: Map<string, NoteFile>): NoteFile | null {
  for (const id of references(body)) {
    const file = files.get(id);
    if (file && isImage(file)) return file;
  }
  return null;
}

export function taskProgress(body: string): { done: number; total: number } {
  const lines = body.replace(/\r\n?/g, "\n").split("\n");
  const tasks = noteTaskLines(body);
  return { done: tasks.filter((i) => /\[[xX]\]/.test(lines[i])).length, total: tasks.length };
}

/** Tick or untick the task on one line of the source; any other line is left as it is. */
export function toggleTask(body: string, line: number): string {
  const lines = body.replace(/\r\n?/g, "\n").split("\n");
  if (!noteTaskLines(body).includes(line)) return body;
  lines[line] = lines[line].replace(/\[( |x|X)\]/, (box) => (box === "[ ]" ? "[x]" : "[ ]"));
  return lines.join("\n");
}

export function wordCount(text: string): number {
  return (text.replace(/```[\s\S]*?```/g, " ").replace(/\]\([^)]*\)/g, "]").match(/[\p{L}\p{N}][\p{L}\p{N}'’-]*/gu) ?? []).length;
}

/** Minutes to read at a calm 200 words a minute, never less than one. */
export function readingMinutes(words: number): number {
  return Math.max(1, Math.round(words / 200));
}

/** Whether a note holds every word of the query, in its title, its words or the names of its files. */
export function matches(note: Note, query: string, files: Map<string, NoteFile>): boolean {
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return true;
  const names = references(note.body).map((id) => files.get(id)?.name ?? "").join(" ");
  const hay = `${note.title}\n${note.body}\n${names}`.toLowerCase();
  return terms.every((term) => hay.includes(term));
}

/** A scope's notes as the panel lists them: pinned first, then the operator's order. */
export function ordered(notes: Note[]): Note[] {
  return [...notes].sort((a, b) => Number(b.pinned) - Number(a.pinned) || a.position - b.position || b.created_at.localeCompare(a.created_at));
}

/** The title a note shows: its own, or its first line of words, or nothing (the card says "Untitled"). */
export function shownTitle(note: Pick<Note, "title" | "body">): string {
  if (note.title.trim()) return note.title.trim();
  const first = note.body.split("\n").find((line) => line.trim() && !/^\s*```/.test(line)) ?? "";
  return excerpt(first, 80);
}

// ── the editor's text commands ───────────────────────────────────────────────────────────────

/** A text with its selection after a command. */
export type Edit = { text: string; start: number; end: number };

function lineStart(text: string, at: number): number {
  return text.lastIndexOf("\n", at - 1) + 1;
}

function lineEnd(text: string, at: number): number {
  const end = text.indexOf("\n", at);
  return end < 0 ? text.length : end;
}

/** Marks around the selection (`**`, `_`, `` ` ``, `~~`), or around a placeholder with nothing selected;
 *  the same marks already there are taken off instead. */
export function wrap(text: string, start: number, end: number, before: string, after = before, placeholder = ""): Edit {
  const inner = text.slice(start, end);
  if (text.slice(start - before.length, start) === before && text.slice(end, end + after.length) === after) {
    return { text: text.slice(0, start - before.length) + inner + text.slice(end + after.length), start: start - before.length, end: end - before.length };
  }
  if (inner.startsWith(before) && inner.endsWith(after) && inner.length >= before.length + after.length) {
    const bare = inner.slice(before.length, inner.length - after.length);
    return { text: text.slice(0, start) + bare + text.slice(end), start, end: start + bare.length };
  }
  const shown = inner || placeholder;
  return { text: text.slice(0, start) + before + shown + after + text.slice(end), start: start + before.length, end: start + before.length + shown.length };
}

/** A link around the selection: the words become the link's text and the address is selected to type. */
export function link(text: string, start: number, end: number, placeholder: string): Edit {
  const inner = text.slice(start, end);
  if (/^https?:\/\/\S+$/.test(inner)) {
    const made = `[${placeholder}](${inner})`;
    return { text: text.slice(0, start) + made + text.slice(end), start: start + 1, end: start + 1 + placeholder.length };
  }
  const words = inner || placeholder;
  const made = `[${words}](https://)`;
  const at = start + words.length + 3;
  return { text: text.slice(0, start) + made + text.slice(end), start: at, end: at + 8 };
}

export type LineKind = "ul" | "ol" | "task" | "quote" | "h1" | "h2" | "h3";
const PREFIX_RE = /^(\s*)(?:[-*+]\s+\[[ xX]\]\s+|[-*+]\s+|\d+[.)]\s+|>\s?|#{1,6}\s+)?/;

function prefixFor(kind: LineKind, n: number): string {
  return kind === "ul" ? "- " : kind === "ol" ? `${n}. ` : kind === "task" ? "- [ ] " : kind === "quote" ? "> " : `${"#".repeat(Number(kind[1]))} `;
}

function hasPrefix(line: string, kind: LineKind): boolean {
  const body = line.replace(/^\s*/, "");
  if (kind === "task") return /^[-*+]\s+\[[ xX]\]\s/.test(body);
  if (kind === "ul") return /^[-*+]\s/.test(body) && !/^[-*+]\s+\[[ xX]\]\s/.test(body);
  if (kind === "ol") return /^\d+[.)]\s/.test(body);
  if (kind === "quote") return /^>/.test(body);
  return new RegExp(`^#{${kind[1]}}\\s`).test(body);
}

/** Every line the selection touches becomes a list item, a task, a quote or a heading, numbered in order
 *  for a numbered list; when every one already is, the mark comes off instead. */
export function linePrefix(text: string, start: number, end: number, kind: LineKind): Edit {
  const from = lineStart(text, start);
  const to = lineEnd(text, Math.max(start, end - (end > start && text[end - 1] === "\n" ? 1 : 0)));
  const lines = text.slice(from, to).split("\n");
  const off = lines.every((line) => !line.trim() || hasPrefix(line, kind));
  let n = 0;
  const changed = lines.map((line) => {
    if (!line.trim() && lines.length > 1) return line;
    const [, indent] = PREFIX_RE.exec(line) as RegExpExecArray;
    const bare = line.replace(PREFIX_RE, "");
    n++;
    return off ? indent + bare : indent + prefixFor(kind, n) + bare;
  });
  const replaced = changed.join("\n");
  const out = text.slice(0, from) + replaced + text.slice(to);
  if (start === end) {
    const caret = Math.max(from, start + (changed[0].length - lines[0].length));
    return { text: out, start: caret, end: caret };
  }
  return { text: out, start: from, end: from + replaced.length };
}

const ITEM_RE = /^(\s*)(?:([-*+])(\s+)(\[[ xX]\]\s+)?|(\d+)([.)])(\s+)(\[[ xX]\]\s+)?|(>\s?))/;

/** Enter inside a list item or a quote: the next line starts the same way (a number one higher, a fresh
 *  box); Enter on an empty item ends the list instead. Null when the caret is in neither. */
export function continueList(text: string, start: number, end: number): Edit | null {
  if (start !== end) return null;
  const from = lineStart(text, start);
  const line = text.slice(from, lineEnd(text, start));
  const m = ITEM_RE.exec(line);
  if (!m) return null;
  const marker = m[0];
  if (start - from < marker.length) return null;
  if (!line.slice(marker.length).trim()) {
    // An empty item: the list ends here, and the line is left empty for the paragraph after it.
    const out = text.slice(0, from) + text.slice(from + line.length);
    return { text: out, start: from, end: from };
  }
  const [, indent, bullet, gap, , num, dot, numGap, , quote] = m;
  const box = m[4] || m[8] ? "[ ] " : "";
  const next = quote ? `${indent}${quote}` : bullet ? `${indent}${bullet}${gap}${box}` : `${indent}${Number(num) + 1}${dot}${numGap}${box}`;
  const insert = `\n${next}`;
  return { text: text.slice(0, start) + insert + text.slice(end), start: start + insert.length, end: start + insert.length };
}

const INDENT = "  ";

/** Tab and Shift+Tab: every line the selection touches moves in or out by one step. */
export function indent(text: string, start: number, end: number, out: boolean): Edit {
  const from = lineStart(text, start);
  const to = lineEnd(text, Math.max(start, end - (end > start && text[end - 1] === "\n" ? 1 : 0)));
  const lines = text.slice(from, to).split("\n");
  let firstShift = 0;
  let total = 0;
  const moved = lines.map((line, i) => {
    if (!out) {
      if (i === 0) firstShift = INDENT.length;
      total += INDENT.length;
      return INDENT + line;
    }
    const remove = line.startsWith(INDENT) ? INDENT.length : line.startsWith(" ") || line.startsWith("\t") ? 1 : 0;
    if (i === 0) firstShift = -remove;
    total -= remove;
    return line.slice(remove);
  });
  const out_ = text.slice(0, from) + moved.join("\n") + text.slice(to);
  return { text: out_, start: Math.max(from, start + firstShift), end: Math.max(from, end + total) };
}

/** A block put in at the caret on a line of its own, with a blank line on each side when it needs one;
 *  `select` is the part of the block to select afterwards (its placeholder). */
export function insertBlock(text: string, start: number, end: number, block: string, select?: [number, number]): Edit {
  const before = text.slice(0, start);
  const after = text.slice(end);
  const lead = !before || before.endsWith("\n\n") ? "" : before.endsWith("\n") ? "\n" : "\n\n";
  const trail = !after ? "\n" : after.startsWith("\n\n") ? "" : after.startsWith("\n") ? "\n" : "\n\n";
  const at = before.length + lead.length;
  const out = before + lead + block + trail + after;
  return select ? { text: out, start: at + select[0], end: at + select[1] } : { text: out, start: at + block.length, end: at + block.length };
}

/** Text put in at the caret, replacing the selection; the caret goes after it. */
export function insertText(text: string, start: number, end: number, snippet: string): Edit {
  return { text: text.slice(0, start) + snippet + text.slice(end), start: start + snippet.length, end: start + snippet.length };
}

/** The word being typed after a trigger at the caret: `[[` for a file, `/` at a line's start for a
 *  command. Null when the caret is not right after one. */
export function trigger(text: string, caret: number): { kind: "file" | "slash"; from: number; query: string } | null {
  const line = text.slice(lineStart(text, caret), caret);
  const file = /\[\[([^\]\n[]{0,40})$/.exec(line);
  if (file) return { kind: "file", from: caret - file[0].length, query: file[1] };
  const slash = /^(\s*)\/([\p{L}\p{N}-]{0,20})$/u.exec(line);
  if (slash) return { kind: "slash", from: caret - slash[0].length + slash[1].length, query: slash[2] };
  return null;
}

// ── drafts on this device ────────────────────────────────────────────────────────────────────

/** What the editor last held for a note, kept on every keystroke and dropped once the host confirms it:
 *  a closed tab, a crash or a lost connection costs nothing. `base` is the version it was typed on. */
export type Draft = { title: string; body: string; base: number; at: number };

const DRAFT_PREFIX = "daedalus.notes.draft.";

export function draftKey(noteId: string | null, sessionId: string, scope: NoteScope): string {
  return DRAFT_PREFIX + (noteId ?? `new.${sessionId}.${scope}`);
}

export function readDraft(key: string, storage: Pick<Storage, "getItem"> | null = safeStorage()): Draft | null {
  try {
    const raw = storage?.getItem(key);
    if (!raw) return null;
    const draft = JSON.parse(raw) as Draft;
    return typeof draft.body === "string" && typeof draft.title === "string" ? draft : null;
  } catch {
    return null;
  }
}

export function writeDraft(key: string, draft: Draft, storage: Pick<Storage, "setItem"> | null = safeStorage()): void {
  try {
    storage?.setItem(key, JSON.stringify(draft));
  } catch {
    /* a full or private storage: the autosave still runs */
  }
}

export function dropDraft(key: string, storage: Pick<Storage, "removeItem"> | null = safeStorage()): void {
  try {
    storage?.removeItem(key);
  } catch {
    /* private mode */
  }
}

const SUBTAB_KEY = "daedalus.notes.subtab";
const MODE_KEY = "daedalus.notes.mode";
export type EditorMode = "write" | "split" | "read";

export function readSubtab(): NoteScope {
  try {
    return localStorage.getItem(SUBTAB_KEY) === "chat" ? "chat" : "project";
  } catch {
    return "project";
  }
}

export function rememberSubtab(scope: NoteScope): void {
  try {
    localStorage.setItem(SUBTAB_KEY, scope);
  } catch {
    /* private mode */
  }
}

/** The editor's layout last chosen for writing; reading an existing note always opens on the reading view. */
export function readMode(wide: boolean): EditorMode {
  try {
    const stored = localStorage.getItem(MODE_KEY);
    if (stored === "write" || (stored === "split" && wide)) return stored;
  } catch {
    /* private mode */
  }
  return wide ? "split" : "write";
}

export function rememberMode(mode: EditorMode): void {
  if (mode === "read") return;
  try {
    localStorage.setItem(MODE_KEY, mode);
  } catch {
    /* private mode */
  }
}

function safeStorage(): Storage | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

// ── export ───────────────────────────────────────────────────────────────────────────────────

/** A note as one Markdown document: its title as the heading, unless the words already open with it. */
export function noteMarkdown(note: Pick<Note, "title" | "body">): string {
  const title = note.title.trim();
  const body = note.body.replace(/\s+$/, "");
  if (!title || body.replace(/^\s+/, "").startsWith(`# ${title}`)) return `${body}\n`;
  return `# ${title}\n\n${body}\n`;
}

/** A name a file system accepts, from a note's title. */
export function fileNameOf(title: string, fallback: string): string {
  const name = title.replace(/[\\/:*?"<>|\u0000-\u001f]+/g, " ").replace(/\s+/g, " ").trim().slice(0, 80);
  return name || fallback;
}

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

export function crc32(data: Uint8Array): number {
  let c = 0xffffffff;
  for (let i = 0; i < data.length; i++) c = CRC_TABLE[(c ^ data[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

/** A zip of the entries, stored without compression: pictures and PDFs are compressed already, and a
 *  library for deflate would be the app's largest dependency for a note's export. Names are UTF-8
 *  (flag 11), so a Russian title stays readable when unpacked. */
export function zip(entries: { name: string; data: Uint8Array }[], at = new Date()): Blob {
  const encoder = new TextEncoder();
  const time = ((at.getHours() << 11) | (at.getMinutes() << 5) | (at.getSeconds() >> 1)) & 0xffff;
  const date = (((at.getFullYear() - 1980) << 9) | ((at.getMonth() + 1) << 5) | at.getDate()) & 0xffff;
  const parts: Uint8Array[] = [];
  const central: Uint8Array[] = [];
  let offset = 0;
  for (const entry of entries) {
    const name = encoder.encode(entry.name);
    const crc = crc32(entry.data);
    const local = new DataView(new ArrayBuffer(30));
    local.setUint32(0, 0x04034b50, true);
    local.setUint16(4, 20, true);
    local.setUint16(6, 0x0800, true);
    local.setUint16(8, 0, true);
    local.setUint16(10, time, true);
    local.setUint16(12, date, true);
    local.setUint32(14, crc, true);
    local.setUint32(18, entry.data.length, true);
    local.setUint32(22, entry.data.length, true);
    local.setUint16(26, name.length, true);
    local.setUint16(28, 0, true);
    parts.push(new Uint8Array(local.buffer), name, entry.data);
    const head = new DataView(new ArrayBuffer(46));
    head.setUint32(0, 0x02014b50, true);
    head.setUint16(4, 20, true);
    head.setUint16(6, 20, true);
    head.setUint16(8, 0x0800, true);
    head.setUint16(10, 0, true);
    head.setUint16(12, time, true);
    head.setUint16(14, date, true);
    head.setUint32(16, crc, true);
    head.setUint32(20, entry.data.length, true);
    head.setUint32(24, entry.data.length, true);
    head.setUint16(28, name.length, true);
    head.setUint32(42, offset, true);
    central.push(new Uint8Array(head.buffer), name);
    offset += 30 + name.length + entry.data.length;
  }
  const size = central.reduce((n, part) => n + part.length, 0);
  const tail = new DataView(new ArrayBuffer(22));
  tail.setUint32(0, 0x06054b50, true);
  tail.setUint16(8, entries.length, true);
  tail.setUint16(10, entries.length, true);
  tail.setUint32(12, size, true);
  tail.setUint32(16, offset, true);
  return new Blob([...parts, ...central, new Uint8Array(tail.buffer)] as BlobPart[], { type: "application/zip" });
}

/** The path a referenced file takes inside an export: exactly the path the note's Markdown names, so
 *  the links resolve once the archive is unpacked. */
export function archivePath(file: Pick<NoteFile, "id" | "name">): string {
  const target = reference({ ...file, mime: "" });
  return decodeURIComponent(target.slice(target.indexOf("](") + 2, -1));
}

// ── the conflict prompt ──────────────────────────────────────────────────────────────────────

export type DiffRow = { kind: "same" | "mine" | "theirs"; text: string };

/** The two versions line by line (a longest common subsequence), for the prompt that asks which to
 *  keep. Past a few thousand lines the table would be too large to build on a phone: the rows are then
 *  simply theirs and mine, one after the other. */
export function lineDiff(theirs: string, mine: string): DiffRow[] {
  const a = theirs.split("\n");
  const b = mine.split("\n");
  if (a.length * b.length > 4_000_000) return [...a.map((text) => ({ kind: "theirs" as const, text })), ...b.map((text) => ({ kind: "mine" as const, text }))];
  const n = a.length;
  const m = b.length;
  const table = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) table[i][j] = a[i] === b[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
  const rows: DiffRow[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      rows.push({ kind: "same", text: a[i] });
      i++;
      j++;
    } else if (table[i + 1][j] >= table[i][j + 1]) rows.push({ kind: "theirs", text: a[i++] });
    else rows.push({ kind: "mine", text: b[j++] });
  }
  while (i < n) rows.push({ kind: "theirs", text: a[i++] });
  while (j < m) rows.push({ kind: "mine", text: b[j++] });
  return rows;
}
