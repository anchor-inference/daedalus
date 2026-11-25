// The files a turn touched, split the way the operator reads them: what the agent handed over on
// purpose (SendFile) and what it merely wrote or edited on the way. The first are cards; the second
// are one line under the answer that opens into a list. Twenty-three cards, one per written file,
// each with the file's absolute path, was a wall the answer disappeared under.

import type { Activity } from "./turns";
import type { SessionFolder } from "./api";
import { bytes } from "./format";

/** A file the turn handed to the operator: drawn as a card. */
export type SentFile = {
  callId: string;
  path: string;
  name: string;
  how: "sent";
  caption: string;
  size: string | null;
  /** Where the workspace or a project folder has it, when it is inside one. */
  place: Place | null;
};

/** A file the turn wrote or edited: one row of the changed list. */
export type ChangedFile = {
  callId: string;
  path: string;
  name: string;
  /** Written whole (created or overwritten) at least once, or only edited. */
  how: "wrote" | "edited";
  place: Place | null;
  /** The lines the turn's calls added and removed in it, or null when one of them cannot be counted. */
  lines: LineChange | null;
};

/** Lines added and removed, as a diff counts them. */
export type LineChange = { added: number; removed: number };

const REPLACED_RE = /:\s*(\d+)\s+replacement/;

function linesOf(text: string): string[] {
  if (!text) return [];
  return (text.endsWith("\n") ? text.slice(0, -1) : text).split("\n");
}

/**
 * One replacement in lines: the lines both sides share at the top and at the bottom are context, the
 * rest of the old text was removed and the rest of the new added. That is what a line diff of the two
 * gives for the one hunk an edit is, without the cost of a real diff on a long block.
 */
export function replacedLines(old: string, now: string): LineChange {
  const a = linesOf(old);
  const b = linesOf(now);
  let top = 0;
  while (top < a.length && top < b.length && a[top] === b[top]) top++;
  let tail = 0;
  while (tail < a.length - top && tail < b.length - top && a[a.length - 1 - tail] === b[b.length - 1 - tail]) tail++;
  return { added: b.length - top - tail, removed: a.length - top - tail };
}

/**
 * What one call changed, in lines, from its own arguments: an Edit's replacements (each as many times
 * as the result says it matched when it replaced every match), a Write's content as lines written. A
 * Write over an existing file is counted as what it wrote: the call does not say what was there. Null
 * for a call that carries no text to count (a patch, an imported tool, arguments cut short).
 */
export function lineChange(name: string, args: Record<string, unknown>, result = ""): LineChange | null {
  if (name === "Write") return typeof args.content === "string" ? { added: linesOf(args.content).length, removed: 0 } : null;
  if (name !== "Edit" && name !== "MultiEdit") return null;
  const edits = Array.isArray(args.edits) ? (args.edits as Record<string, unknown>[]) : [args];
  let added = 0;
  let removed = 0;
  for (const edit of edits) {
    const old = edit?.old_string ?? edit?.old;
    const now = edit?.new_string ?? edit?.new;
    if (typeof old !== "string" || typeof now !== "string") return null;
    const change = replacedLines(old, now);
    // Every match replaced: the result's count is how many times this change landed. Only a single
    // edit can say so; a batch reports one total for all of its edits.
    const times = edit?.replace_all === true && edits.length === 1 ? Number(REPLACED_RE.exec(result)?.[1] ?? 1) : 1;
    added += change.added * times;
    removed += change.removed * times;
  }
  return { added, removed };
}

/** The sum of changes, null as soon as one of them could not be counted. */
export function sumLines(changes: readonly (LineChange | null)[]): LineChange | null {
  let added = 0;
  let removed = 0;
  for (const change of changes) {
    if (!change) return null;
    added += change.added;
    removed += change.removed;
  }
  return { added, removed };
}

/** A path as the session's folders know it: relative to the workspace (`folder` empty) or to another folder of its project. */
export type Place = { rel: string; folder: string };

const WRITERS = new Set(["Write", "Edit", "MultiEdit"]);
const SIZE_RE = /\(([\d.,]+\s?[KMG]?B)\)/i;
const BYTES_RE = /\((\d+) bytes\)/i;

/** Forward slashes, no doubled or trailing separators: a Windows path and a POSIX one compare alike. */
function normal(path: string): string {
  const slashed = path.trim().replace(/\\/g, "/").replace(/\/{2,}/g, "/");
  return slashed.length > 1 ? slashed.replace(/\/+$/, "") : slashed;
}

const DRIVE_RE = /^[a-zA-Z]:\//;

function isAbsolute(path: string): boolean {
  return path.startsWith("/") || path === "~" || path.startsWith("~/") || DRIVE_RE.test(path);
}

/**
 * `path` relative to `root`, or null when it is not inside. A drive-letter path compares without case,
 * as Windows does. A path written from the home folder (`~/AppData/…`) — which is how a model on
 * Windows tends to spell the workspace — is matched by the longest tail of the root it starts with:
 * the root itself is absolute and never begins with a tilde, so a plain prefix test missed every one
 * and the card showed the whole path.
 */
function within(path: string, root: string): string | null {
  if (!root) return null;
  const fold = DRIVE_RE.test(root) || DRIVE_RE.test(path);
  const p = fold ? path.toLowerCase() : path;
  const r = fold ? root.toLowerCase() : root;
  if (p === r) return "";
  if (p.startsWith(r.endsWith("/") ? r : `${r}/`)) return path.slice(r.length + (r.endsWith("/") ? 0 : 1));
  if (p.startsWith("~/")) {
    const rest = path.slice(2);
    const restFold = p.slice(2);
    const segments = r.split("/");
    for (let i = 1; i < segments.length; i++) {
      const tail = segments.slice(i).join("/");
      if (tail && restFold.startsWith(`${tail}/`)) return rest.slice(tail.length + 1);
    }
  }
  return null;
}

/**
 * Where a tool's path is among the session's folders. A relative path is the workspace's, as the
 * tools resolve it; an absolute one is placed in the innermost of the workspace and the project's
 * other folders that holds it; anything else has no place, and the row shows it as the tool wrote it.
 */
export function placeOf(path: string, workspace: string, folders: readonly Pick<SessionFolder, "id" | "path">[] = []): Place | null {
  const p = normal(path);
  if (!p) return null;
  if (!isAbsolute(p)) return { rel: p.replace(/^(\.\/)+/, ""), folder: "" };
  const home = within(p, normal(workspace));
  let best: Place | null = home ? { rel: home, folder: "" } : null;
  // The innermost folder wins, whichever order they come in: a staff worktree lives inside the
  // project folder (`.agents/worktrees/<name>`), and a file the member wrote there, placed in the
  // project folder, opened the project's copy of the path — a different file, or none at all.
  for (const folder of folders) {
    const rel = within(p, normal(folder.path));
    if (rel && (!best || rel.length < best.rel.length)) best = { rel, folder: folder.id };
  }
  return best;
}

/**
 * A screenshot the agent sent in the same step (`BrowserScreenshot(send=true)`) is a sent file like
 * SendFile's, but its path is the one the host chose, so it is read from the result's first line.
 */
const SCREENSHOT = "BrowserScreenshot";
const SAVED_RE = /^Saved .+? to (.+)\.$/m;

/**
 * The file a call put in front of the operator. A folder sent with `zip` arrives as the archive the host
 * writes beside it under the folder's name, so the card names and opens that, not the folder.
 */
export function sentPath(name: string, args: Record<string, unknown> | undefined, result = ""): string {
  if (name === SCREENSHOT) return args?.send === true ? SAVED_RE.exec(result)?.[1] ?? "" : "";
  const path = typeof args?.path === "string" ? args.path : "";
  return name === "SendFile" && path && args?.zip === true ? `${path.replace(/[\\/]+$/, "")}.zip` : path;
}

function nameOf(path: string): string {
  const p = normal(path);
  return p.split("/").filter(Boolean).pop() ?? p;
}

const keyOf = (path: string, place: Place | null) => (place ? `${place.folder}:${place.rel}` : `?:${normal(path)}`);

/**
 * The files of a turn, each once: the sent ones in the order they were sent, the changed ones in the
 * order they were first touched. A file both written and sent is a card only — listing it again
 * among the changed files is the repetition the list exists to remove. Failed and running calls are
 * left out: a write that was refused changed nothing.
 */
export function turnFiles(items: readonly Activity[], workspace = "", folders: readonly Pick<SessionFolder, "id" | "path">[] = []): { sent: SentFile[]; changed: ChangedFile[] } {
  const sent = new Map<string, SentFile>();
  const changed = new Map<string, ChangedFile>();
  for (const a of items) {
    if (a.kind !== "tool" || a.running || a.error) continue;
    if (a.name !== "SendFile" && a.name !== SCREENSHOT && !WRITERS.has(a.name)) continue;
    const path = sentPath(a.name, a.args, a.result ?? "");
    if (!path) continue;
    const place = placeOf(path, workspace, folders);
    const key = keyOf(path, place);
    if (a.name === "SendFile" || a.name === SCREENSHOT) {
      const caption = typeof a.args.caption === "string" ? a.args.caption : "";
      const counted = BYTES_RE.exec(a.result ?? "");
      const size = counted ? bytes(Number(counted[1])) : SIZE_RE.exec(a.result ?? "")?.[1] ?? null;
      sent.delete(key);
      sent.set(key, { callId: a.id, path, name: nameOf(path), how: "sent", caption, size, place });
      continue;
    }
    const before = changed.get(key);
    const how = a.name === "Write" || before?.how === "wrote" ? "wrote" : "edited";
    const lines = sumLines([before ? before.lines : { added: 0, removed: 0 }, lineChange(a.name, a.args, a.result ?? "")]);
    changed.set(key, { callId: before?.callId ?? a.id, path, name: nameOf(path), how, place, lines });
  }
  for (const key of sent.keys()) changed.delete(key);
  return { sent: [...sent.values()], changed: [...changed.values()] };
}

/** The cards drawn before "+N more": enough to see what came, few enough not to bury the answer. */
export const SENT_VISIBLE = 3;

/**
 * A page's slug as the host defines it (daedalus/slugs.py): lower-case letters of any script, digits,
 * combining marks and hyphens, starting with a letter or digit. A Latin-only test dropped the card of
 * every page whose title was Russian once the host kept those letters.
 */
export function isPageSlug(slug: string): boolean {
  return /^[\p{Ll}\p{Lm}\p{Lo}\p{Nd}][\p{Ll}\p{Lm}\p{Lo}\p{Nd}\p{Mn}\p{Mc}-]{0,62}$/u.test(slug) && slug === slug.normalize("NFC");
}

/** A page the turn showed the operator (PageShow): drawn as a card that opens it. */
export type ShownPage = { callId: string; slug: string; draft: boolean };

/**
 * The pages a turn showed, each once, the last showing of a page winning (it may have moved from
 * the draft to the live version in between). Only finished calls count: a refused PageShow named a
 * page that is not there, and a running one has not shown anything yet.
 */
export function turnPages(items: readonly Activity[]): ShownPage[] {
  const shown = new Map<string, ShownPage>();
  for (const a of items) {
    if (a.kind !== "tool" || a.name !== "PageShow" || a.running || a.error) continue;
    const slug = typeof a.args.slug === "string" ? a.args.slug.trim() : "";
    if (!isPageSlug(slug)) continue;
    shown.delete(slug);
    shown.set(slug, { callId: a.id, slug, draft: a.args.draft === true });
  }
  return [...shown.values()];
}
