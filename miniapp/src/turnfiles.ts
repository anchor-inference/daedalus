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
};

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
 * tools resolve it; an absolute one is placed in the workspace first, then in the project's other
 * folders; anything else has no place, and the row shows it as the tool wrote it.
 */
export function placeOf(path: string, workspace: string, folders: readonly Pick<SessionFolder, "id" | "path">[] = []): Place | null {
  const p = normal(path);
  if (!p) return null;
  if (!isAbsolute(p)) return { rel: p.replace(/^(\.\/)+/, ""), folder: "" };
  const home = within(p, normal(workspace));
  if (home) return { rel: home, folder: "" };
  for (const folder of folders) {
    const rel = within(p, normal(folder.path));
    if (rel) return { rel, folder: folder.id };
  }
  return null;
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
    if (a.name !== "SendFile" && !WRITERS.has(a.name)) continue;
    const path = typeof a.args.path === "string" ? a.args.path : "";
    if (!path) continue;
    const place = placeOf(path, workspace, folders);
    const key = keyOf(path, place);
    if (a.name === "SendFile") {
      const caption = typeof a.args.caption === "string" ? a.args.caption : "";
      const counted = BYTES_RE.exec(a.result ?? "");
      const size = counted ? bytes(Number(counted[1])) : SIZE_RE.exec(a.result ?? "")?.[1] ?? null;
      sent.delete(key);
      sent.set(key, { callId: a.id, path, name: nameOf(path), how: "sent", caption, size, place });
      continue;
    }
    const before = changed.get(key);
    const how = a.name === "Write" || before?.how === "wrote" ? "wrote" : "edited";
    changed.set(key, { callId: before?.callId ?? a.id, path, name: nameOf(path), how, place });
  }
  for (const key of sent.keys()) changed.delete(key);
  return { sent: [...sent.values()], changed: [...changed.values()] };
}

/** The cards drawn before "+N more": enough to see what came, few enough not to bury the answer. */
export const SENT_VISIBLE = 3;
