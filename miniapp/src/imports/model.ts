// Importing a session from another program: the shapes the host answers with, and the rules the
// explorer, the progress view and the imported chat draw from them.
//
// The host reads the other programs' stores on the machine (Claude Code, Codex, Gemini CLI and the
// rest) through its terminal daemon, so everything here is a description of files the app never
// sees. The rules live apart from the screens because they are the part a mistake is expensive in:
// a session shown in the wrong folder, or a destination that says "new project" for a folder that is
// already one, is acted on by the operator before anything can say it was wrong.

import { bytes as fmtBytes, tokens as fmtTokens } from "../format";
import { plural, t } from "../i18n";
import type { MessageView } from "../api";

/** A program the host knows how to read, and what it found of it on the machine (`sessions.harnesses`). */
export type HarnessView = {
  id: string;
  name: string;
  /** Whether the program's store exists on the machine at all. */
  found: boolean;
  sessions: number;
  folders: number;
  root?: string;
  version?: string;
};

/** A project that owns a folder, as the host decorates a folder or a session with it: a project, a
 *  chat's own project, or one of the installation's. */
export type Owner = { id: string; name: string; kind?: "project" | "chat" | "service"; ephemeral?: boolean };

/** The Daedalus chat a session was imported as. */
export type ImportedAs = { session_id: string; title: string };

/** One session of another program, as the host's scan describes it: a header, never the transcript. */
export type ForeignSession = {
  harness: string;
  id: string;
  cwd: string;
  title: string;
  started_at: string;
  updated_at: string;
  /** Visible messages: the operator's and the model's, without tool results and bookkeeping. */
  messages: number;
  bytes: number;
  branch?: string;
  model?: string;
  flags?: { compacted?: number; sidechains?: number; live?: boolean; imported_as?: string | null };
  imported_as?: ImportedAs | null;
  /** The project whose folder the session's folder is, or is inside. */
  project?: Owner | null;
  /** A deep search's matching passage. */
  snippet?: string;
};

/** A folder with sessions somewhere inside it, or (`empty`) one beside them that has none. */
export type FolderCount = { name?: string; path: string; sessions: number; latest?: string | null; project?: Owner | null; empty?: boolean };

export type Place = { name: string; path: string; kind?: string };

/** `GET /api/imports/scan`: one folder of one program, or every match of a search. */
export type ScanResult = {
  path: string;
  home?: string;
  parent?: string | null;
  crumbs?: { name: string; path: string }[];
  /** The sessions whose folder is `path`; with a query, every match in every folder. */
  here: ForeignSession[];
  /** Folders inside `path`, with how many sessions each holds anywhere below it. */
  children: FolderCount[];
  /** Every folder the program has sessions in, freshest first: "Where sessions are". */
  folders: FolderCount[];
  roots?: Place[];
  truncated?: boolean;
  cursor?: string;
};

/** `GET /api/imports/harnesses`. `recent` is the start screen's card, when the host offers it. */
export type HarnessList = { harnesses: HarnessView[]; recent?: ForeignSession[]; home?: string };

export type ImportMode = "full" | "tail";

/** One exchange the preview quotes. */
export type Brief = { role: string; text: string; at?: string };

export type DestinationKind = "project" | "chat" | "new_chat" | "new_project" | "refused";

/** `GET /api/imports/preview`: what the session holds, how it would come over and where it would land. */
export type ImportPreview = {
  header: ForeignSession;
  harness_name?: string;
  first: Brief[];
  /** Empty when the preview did not read the session to its end. */
  last: Brief[];
  complete?: boolean;
  counts: { turns?: number; user?: number; assistant?: number; tool_calls?: number; compactions?: number; sidechains?: number; images?: number };
  /** The working history's messages, its size in the proposed model's tokens, and that model's window. */
  messages: number;
  tokens: number;
  window: number;
  /** "tail" for a session too large to start from whole. */
  suggested_mode: ImportMode;
  /** The preset the import runs on: the source's own model when there is a preset for it, else the default. */
  model: { source?: string; preset?: string | null; label?: string; same?: boolean; window?: number };
  models?: { id: string; label: string; window: number }[];
  destination: {
    kind: DestinationKind;
    cwd: string;
    project?: Owner | null;
    worktree_cwd?: string | null;
    /** Why it cannot land there, when `kind` is "refused": missing, contains_project, inside_project, installation. */
    code?: string | null;
    problem?: string | null;
    other_project?: Owner | null;
    exists?: boolean | null;
    becomes_project?: boolean;
  };
  imported_as?: ImportedAs | null;
  /** The other program is still writing it. */
  live?: boolean;
  masked?: number;
};

export type StageId = "read" | "parse" | "mask" | "write" | "index" | "summarise" | "open";
export type StageState = "todo" | "now" | "done" | "failed";

/** One line of the progress view, derived from the job's current stage and counts. */
export type Stage = { id: StageId; state: StageState };

/** `GET /api/imports/{job_id}` (and `POST /api/imports`'s `job`): where an import is and what it counted. */
export type ImportJob = {
  job_id: string;
  state: "running" | "done" | "failed";
  stage: StageId;
  stages?: StageId[];
  counts: { turns_read?: number; turns_total?: number; messages?: number; masked?: number; written?: number; gap?: number; history?: number; parts_done?: number; parts_total?: number };
  session_id?: string | null;
  error?: { code: string; message: string } | null;
};

/** Where an imported chat came from, as the session's detail carries it. */
export type ImportedOrigin = {
  harness: string;
  harness_name?: string;
  /** The session's id in its own program. */
  id: string;
  cwd?: string;
  source_model?: string;
  branch?: string;
  mode?: ImportMode;
  /** The other program is still writing the session: "Pull in what's new" has something to pull. */
  live?: boolean;
  complete?: boolean;
  imported_at: string;
  refreshed_at?: string | null;
  masked: number;
  counts: { turns?: number; user?: number; assistant?: number; tool_calls?: number; compactions?: number; sidechains?: number };
  transcript_dropped?: number;
  /** The masked original, when it was kept. */
  original?: { stored: boolean; bytes?: number | null; name?: string | null; reason?: string | null };
};

/** The marker the host puts on a message that came from another program: which program, and the
 *  names its tool calls had there, by call id. */
export type MessageImport = { harness: string; ext_id?: string; seq?: number; sidechain?: string; model?: string; tools?: Record<string, string>; text_tools?: string[]; summary?: boolean };

/** The order of the stages, as the progress view lists them. */
export const STAGES: StageId[] = ["read", "parse", "mask", "write", "index", "summarise", "open"];

/** The programs the explorer knows a mark and a colour for. A brand name is written as its own
 *  documentation writes it; the host's `name` wins when it gives one. */
const HARNESSES: Record<string, { name: string; mark: string; color: string }> = {
  claude: { name: "Claude Code", mark: "CC", color: "var(--staff-orange)" },
  codex: { name: "Codex", mark: "CX", color: "var(--staff-teal)" },
  gemini: { name: "Gemini CLI", mark: "GE", color: "var(--staff-blue)" },
  opencode: { name: "opencode", mark: "OC", color: "var(--staff-slate)" },
  pi: { name: "pi", mark: "π", color: "var(--staff-violet)" },
  grok: { name: "Grok CLI", mark: "GK", color: "var(--staff-rose)" },
  cursor: { name: "Cursor", mark: "CU", color: "var(--staff-green)" },
  qwen: { name: "Qwen Code", mark: "QW", color: "var(--staff-amber)" },
  aider: { name: "aider", mark: "AI", color: "var(--staff-green)" },
  goose: { name: "goose", mark: "GO", color: "var(--staff-amber)" },
  crush: { name: "Crush", mark: "CR", color: "var(--staff-rose)" },
};

/** A program's name, mark and colour; an unknown one gets its own id and a neutral tile. */
export function harnessMeta(id: string, name?: string): { name: string; mark: string; color: string } {
  const known = HARNESSES[id];
  if (known) return { ...known, name: name || known.name };
  const label = name || id;
  return { name: label, mark: label.slice(0, 2).toUpperCase(), color: "var(--staff-slate)" };
}

/** The strip's order: the programs found, the ones with sessions first, then the ones not installed. */
export function harnessOrder(list: HarnessView[]): HarnessView[] {
  return [...list].sort((a, b) => Number(b.found) - Number(a.found) || Number(b.sessions > 0) - Number(a.sessions > 0));
}

/** The program the explorer opens on: the one asked for, else the first with sessions. */
export function firstHarness(list: HarnessView[], asked?: string | null): string {
  if (asked && list.some((h) => h.id === asked)) return asked;
  return harnessOrder(list).find((h) => h.found && h.sessions > 0)?.id ?? list[0]?.id ?? "";
}

function trimSlash(path: string): string {
  return path.length > 1 ? path.replace(/[\\/]+$/, "") : path;
}

/** A path the operator reads: under their home it is `~/…`. */
export function shortPath(path: string, home?: string): string {
  const p = trimSlash(path);
  const h = home ? trimSlash(home) : "";
  if (h && p === h) return "~";
  if (h && (p.startsWith(h + "/") || p.startsWith(h + "\\"))) return "~" + p.slice(h.length).replace(/\\/g, "/");
  return p;
}

/** The last part of a path, which is how a folder is named in a list. */
export function baseName(path: string): string {
  const parts = trimSlash(path).split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] ?? path;
}

/** The breadcrumbs of a path, each with the path it leads to; under the home they start at `~`. */
export function crumbs(path: string, home?: string): { name: string; path: string }[] {
  if (!path) return home ? [{ name: "~", path: home }] : [];
  const p = trimSlash(path).replace(/\\/g, "/");
  const h = home ? trimSlash(home).replace(/\\/g, "/") : "";
  const out: { name: string; path: string }[] = [];
  let rest = p;
  let base = "";
  if (h && (p === h || p.startsWith(h + "/"))) {
    out.push({ name: "~", path: h });
    rest = p.slice(h.length);
    base = h;
  } else if (/^[A-Za-z]:/.test(p)) {
    base = p.slice(0, 2);
    out.push({ name: base, path: base + "/" });
    rest = p.slice(2);
  } else {
    out.push({ name: "/", path: "/" });
  }
  for (const part of rest.split("/").filter(Boolean)) {
    base = `${base}/${part}`;
    out.push({ name: part, path: base });
  }
  return out;
}

/** The folder above, or null at a root. */
export function parentOf(path: string): string | null {
  const p = trimSlash(path).replace(/\\/g, "/");
  if (p === "/" || /^[A-Za-z]:\/?$/.test(p)) return null;
  const at = p.lastIndexOf("/");
  if (at < 0) return null;
  if (at === 0) return "/";
  if (/^[A-Za-z]:$/.test(p.slice(0, at))) return p.slice(0, at) + "/";
  return p.slice(0, at);
}

/** A search's matches grouped by the folder they are in, in the order the host ranked them: the
 *  folder of the best match first, and every match of a folder under it. */
export function groupByFolder(sessions: ForeignSession[]): { cwd: string; sessions: ForeignSession[] }[] {
  const groups: { cwd: string; sessions: ForeignSession[] }[] = [];
  const at = new Map<string, number>();
  for (const s of sessions) {
    const key = trimSlash(s.cwd);
    let index = at.get(key);
    if (index === undefined) {
      index = groups.length;
      at.set(key, index);
      groups.push({ cwd: key, sessions: [] });
    }
    groups[index].sessions.push(s);
  }
  return groups;
}

/** Sessions that are only bookkeeping — an `/init`, a summary, fewer than two messages — go last,
 *  folded: the operator never wants to continue them and they push the real ones down. */
export function isTrivial(s: Pick<ForeignSession, "messages" | "title">): boolean {
  return s.messages < 2 || /^\/(init|clear|compact)\b/.test(s.title.trim());
}

export function splitTrivial(sessions: ForeignSession[]): { main: ForeignSession[]; trivial: ForeignSession[] } {
  const main: ForeignSession[] = [];
  const trivial: ForeignSession[] = [];
  for (const s of sessions) (isTrivial(s) ? trivial : main).push(s);
  return { main, trivial };
}

/** A size a session is warned about: the import will take a while and should probably be a summary. */
export const LARGE_BYTES = 16 * 1024 * 1024;

export function isLargeSession(s: Pick<ForeignSession, "bytes" | "messages">): boolean {
  return s.bytes >= LARGE_BYTES || s.messages >= 2000;
}

/** The window of the model the import would run on: the one the operator picked, else the host's. */
export function windowFor(p: Pick<ImportPreview, "window" | "models">, model: string): number {
  return p.models?.find((m) => m.id === model)?.window ?? p.window;
}

/** Whether a preview is of a session too large to come over whole: the host's own rule (more than
 *  60 % of the window, or more than two thousand messages) against the model chosen here. */
export function isLargePreview(p: Pick<ImportPreview, "tokens" | "window" | "messages" | "models">, model = ""): boolean {
  const window = windowFor(p, model);
  return (window > 0 && p.tokens > window * 0.6) || p.messages > 2000;
}

/** The mode the window proposes: the host's suggestion, unless the operator picked a model whose window changes it. */
export function suggestedMode(p: ImportPreview, model = ""): ImportMode {
  if (!model || model === p.model.preset) return p.suggested_mode;
  return isLargePreview(p, model) ? "tail" : "full";
}

/** The working history's share of the model's window, in whole percent, or null when it is unknown. */
export function windowShare(p: Pick<ImportPreview, "tokens" | "window" | "models">, model = ""): number | null {
  const window = windowFor(p, model);
  return window > 0 ? Math.round((100 * p.tokens) / window) : null;
}

/** The chat a session was imported as, from the scan's object or its flag. */
export function importedId(s: Pick<ForeignSession, "imported_as" | "flags">): string | null {
  return s.imported_as?.session_id || s.flags?.imported_as || null;
}

/** The meta line of a session row: id, branch, messages, compactions, subagents, size. */
export function sessionMeta(s: ForeignSession): string[] {
  return [
    shortId(s.id),
    s.branch ?? "",
    plural("imp.messages.short", s.messages),
    s.flags?.compacted ? plural("imp.compactions", s.flags.compacted) : "",
    s.flags?.sidechains ? plural("imp.subagents", s.flags.sidechains) : "",
    s.bytes ? fmtBytes(s.bytes) : "",
  ].filter(Boolean);
}

/** A session id as a row shows it: a uuid's first block, a long id's ends. */
export function shortId(id: string): string {
  if (/^[0-9a-f]{8}-[0-9a-f]{4}-/i.test(id)) {
    // A time-ordered uuid (Codex, Grok) shares its first block with every session of the same hour;
    // its end is what tells two apart.
    return /^[0-9a-f]{8}-[0-9a-f]{4}-7/i.test(id) ? `${id.slice(0, 4)}…${id.slice(-4)}` : id.slice(0, 8);
  }
  return id.length > 12 ? `${id.slice(0, 6)}…${id.slice(-4)}` : id;
}

/** A matching passage with its match marked: `[[…]]` when the host marks it, else every occurrence
 *  of the words searched for. */
export function snippetParts(text: string, query = ""): { text: string; hit: boolean }[] {
  if (!text.includes("[[")) return markQuery(text, query);
  const out: { text: string; hit: boolean }[] = [];
  const re = /\[\[([\s\S]*?)\]\]/g;
  let last = 0;
  for (const m of text.matchAll(re)) {
    if (m.index! > last) out.push({ text: text.slice(last, m.index), hit: false });
    out.push({ text: m[1], hit: true });
    last = m.index! + m[0].length;
  }
  if (last < text.length) out.push({ text: text.slice(last), hit: false });
  return out;
}

/** A title with the query marked, for the instant search over titles. */
export function markQuery(text: string, query: string): { text: string; hit: boolean }[] {
  const q = query.trim();
  if (!q) return [{ text, hit: false }];
  const lower = text.toLowerCase();
  const needle = q.toLowerCase();
  const out: { text: string; hit: boolean }[] = [];
  let at = 0;
  for (let i = lower.indexOf(needle); i >= 0; i = lower.indexOf(needle, i + needle.length)) {
    if (i > at) out.push({ text: text.slice(at, i), hit: false });
    out.push({ text: text.slice(i, i + needle.length), hit: true });
    at = i + needle.length;
  }
  if (at < text.length) out.push({ text: text.slice(at), hit: false });
  return out;
}

/** What the destination line says: the project it joins, the chat it makes a project, or the new
 *  chat or project it starts; a refusal says why. */
export function destinationLine(p: ImportPreview, makeProject: boolean, home?: string): { title: string; sub: string } {
  const d = p.destination;
  const where = shortPath(d.cwd, home);
  const name = baseName(d.cwd);
  if (d.kind === "refused") return { title: t("imp.dest.refused"), sub: d.problem || where };
  if (d.kind === "project" && d.project) return { title: t("imp.dest.join", { name: d.project.name }), sub: d.worktree_cwd ? t("imp.dest.join.inside", { path: where }) : t("imp.dest.join.sub") };
  if (d.kind === "chat" && d.project) return { title: t("imp.dest.chat", { name: d.project.name }), sub: t("imp.dest.join.chat", { path: where }) };
  return makeProject || d.kind === "new_project"
    ? { title: t("imp.dest.newproject", { name }), sub: where }
    : { title: t("imp.dest.newchat"), sub: t("imp.dest.newchat.sub", { path: where }) };
}

/** Whether "make it a project now" applies: only an import that starts a chat of its own. One that
 *  joins a folder's chat already makes it a project, the way a second chat there always has. */
export function offersProject(p: ImportPreview): boolean {
  return p.destination.kind === "new_chat" || p.destination.kind === "new_project";
}

/** Whether the preview allows an import at all: a refused destination does not. */
export function canLand(p: ImportPreview): boolean {
  return p.destination.kind !== "refused";
}

/** The stages to list, each with its state: the ones before the job's stage done, its own running
 *  (done once the job is), the rest to come. A summary is listed only for a "tail" import. */
export function jobStages(job: Pick<ImportJob, "stage" | "state" | "stages">, mode: ImportMode): Stage[] {
  const order = (job.stages?.length ? job.stages : STAGES).filter((id) => id !== "summarise" || mode === "tail" || job.stage === "summarise");
  const at = order.indexOf(job.stage);
  return order.map((id, index) => ({
    id,
    state: job.state === "done" ? "done" : index < at ? "done" : index === at ? (job.state === "failed" ? "failed" : "now") : "todo",
  }));
}

/** One stage's line, in the reader's language, with the numbers the job counted. */
export function stageLine(stage: Stage, counts: ImportJob["counts"], destination?: string): { text: string; aside: string } {
  const of = (done?: number, total?: number) => (total ? `${(done ?? 0).toLocaleString()} / ${total.toLocaleString()}` : done ? done.toLocaleString() : "");
  switch (stage.id) {
    case "read":
      return { text: t(stage.state === "done" ? "imp.stage.read.done" : "imp.stage.read"), aside: stage.state === "done" ? plural("imp.turns", counts.turns_read ?? counts.turns_total ?? 0) : of(counts.turns_read, counts.turns_total) };
    case "parse":
      return { text: stage.state === "done" && counts.messages !== undefined ? t("imp.stage.parse.done", { n: plural("imp.messages", counts.messages) }) : t("imp.stage.parse"), aside: "" };
    case "mask":
      return { text: stage.state === "done" ? t("imp.stage.mask.done", { n: counts.masked ?? 0 }) : t("imp.stage.mask"), aside: "" };
    case "write":
      return { text: t("imp.stage.write"), aside: of(counts.written, counts.messages) };
    case "index":
      return { text: t("imp.stage.index"), aside: "" };
    case "summarise":
      return { text: t("imp.stage.summarise"), aside: counts.parts_total ? of(counts.parts_done, counts.parts_total) : counts.gap ? plural("imp.messages", counts.gap) : "" };
    case "open":
      return { text: destination ? t("imp.stage.open.in", { name: destination }) : t("imp.stage.open"), aside: "" };
  }
}

/** How far the import is, 0–100: done stages count whole, the writing one by its own count. */
export function progress(job: Pick<ImportJob, "stage" | "state" | "stages" | "counts">, mode: ImportMode): number {
  if (job.state === "done") return 100;
  const stages = jobStages(job, mode);
  if (!stages.length) return 0;
  let sum = stages.filter((s) => s.state === "done").length;
  const c = job.counts;
  if (job.stage === "write" && c.messages) sum += Math.min(1, (c.written ?? 0) / c.messages);
  if (job.stage === "read" && c.turns_total) sum += Math.min(1, (c.turns_read ?? 0) / c.turns_total);
  if (job.stage === "summarise" && c.parts_total) sum += Math.min(1, (c.parts_done ?? 0) / c.parts_total);
  return Math.round((100 * sum) / stages.length);
}

/** A preview's three numbers: messages, calls and tokens. */
export function previewStats(p: ImportPreview): { value: string; label: string }[] {
  const calls = p.counts.tool_calls ?? 0;
  return [
    { value: p.messages.toLocaleString(), label: plural("imp.stat.messages", p.messages) },
    { value: calls.toLocaleString(), label: plural("imp.stat.calls", calls) },
    { value: `~${fmtTokens(p.tokens)}`, label: t("imp.stat.tokens") },
  ];
}

/** The first thing the operator asked, and the last thing the model answered, as the preview quotes them. */
export function exchanges(p: Pick<ImportPreview, "first" | "last">): { first: Brief | null; last: Brief | null } {
  const first = p.first.find((b) => b.role === "user") ?? p.first[0] ?? null;
  const last = [...p.last].reverse().find((b) => b.role === "assistant") ?? null;
  return { first, last };
}

/** How long ago the other program last wrote a running session, in seconds. */
export function lastWriteSeconds(s: Pick<ForeignSession, "updated_at">, now = Date.now()): number {
  return Math.max(0, Math.round((now - (Date.parse(s.updated_at) || now)) / 1000));
}

/** The import a message came from, when it did. */
export function messageImport(m: Pick<MessageView, "imported">): MessageImport | null {
  const value = m.imported;
  return value && typeof value === "object" && typeof value.harness === "string" ? value : null;
}

/** The name a foreign tool call had in its own program, when the host renamed it to a native one;
 *  null for a call that was Daedalus's own. */
export function foreignToolName(m: Pick<MessageView, "imported">, callId: string): string | null {
  return messageImport(m)?.tools?.[callId] ?? null;
}

/** How many messages an imported chat holds as the source counted them: the operator's and the model's. */
export function originMessages(o: Pick<ImportedOrigin, "counts">): number {
  return (o.counts.user ?? 0) + (o.counts.assistant ?? 0);
}

const DISMISSED = "daedalus.imports.card.dismissed";

/** Whether the start screen's card was closed: the operator closed it for good on this device. */
export function cardDismissed(): boolean {
  try { return localStorage.getItem(DISMISSED) === "1"; } catch { return false; }
}

export function dismissCard(): void {
  try { localStorage.setItem(DISMISSED, "1"); } catch { /* private mode: it comes back next time */ }
}

/** How fresh a session must be to be offered on the start screen. */
export const FRESH_MS = 3 * 24 * 3600 * 1000;

/** The start screen's card: up to three sessions, freshest first, never one already imported, one
 *  still being written, or one older than Daedalus's own last activity in the same project. */
export function cardSessions(found: ForeignSession[] | undefined, lastActivity: Record<string, string> = {}, now = Date.now(), limit = 3): ForeignSession[] {
  return (found ?? [])
    .filter((s) => !importedId(s) && !s.flags?.live && now - (Date.parse(s.updated_at) || 0) < FRESH_MS)
    .filter((s) => !s.project || !lastActivity[s.project.id] || Date.parse(lastActivity[s.project.id]) < Date.parse(s.updated_at))
    .sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at))
    .slice(0, limit);
}

/** The scan request for a folder or a search; `cursor` asks for the page after a truncated answer. */
export function scanUrl(harness: string, path: string, query: string, deep: boolean, cursor = ""): string {
  const params = new URLSearchParams({ harness, path });
  if (query.trim()) params.set("q", query.trim());
  if (deep && query.trim()) params.set("deep", "1");
  if (cursor) params.set("cursor", cursor);
  return `/api/imports/scan?${params.toString()}`;
}

/** A truncated scan with the page after it appended: the folders and the crumbs are the first
 *  page's, the sessions are both pages' (one session never twice), and whether more remain is the
 *  later page's to say. */
export function mergeScan(first: ScanResult, next: ScanResult): ScanResult {
  const seen = new Set(first.here.map((s) => `${s.harness}:${s.id}`));
  return { ...first, here: [...first.here, ...next.here.filter((s) => !seen.has(`${s.harness}:${s.id}`))], truncated: next.truncated, cursor: next.cursor };
}

export function previewUrl(harness: string, id: string): string {
  return `/api/imports/preview?${new URLSearchParams({ harness, id }).toString()}`;
}

/** The body of `POST /api/imports`. */
export function importBody(p: ImportPreview, mode: ImportMode, model: string, makeProject: boolean, again = false): Record<string, unknown> {
  return {
    harness: p.header.harness,
    id: p.header.id,
    mode,
    ...(model ? { model } : {}),
    ...((p.destination.kind === "project" || p.destination.kind === "chat") && p.destination.project ? { project_id: p.destination.project.id } : {}),
    ...(makeProject && offersProject(p) ? { make_project: true } : {}),
    ...(again ? { again: true } : {}),
  };
}
