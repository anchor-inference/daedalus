// The files orchestration keeps by handle (`att:<id>`): wherever a chat names one — the operator's
// attachment, an event card, an orchestrator's answer — the app draws it as a file card that
// downloads and previews. The text keeps the handle, which is what the models pass on; the card is
// the operator's way to the bytes, whichever machine they came from.

/** A handle as the host writes it into a message: `att:` and twelve hex characters. */
export const HANDLE_RE = /\batt:([0-9a-f]{12})\b/g;

export type KeptFile = { id: string; handle: string; name: string; mime: string; size: number; sha256: string; origin: string; created_at: string };

/** The ids of the handles a text names, in order, each once. */
export function handleIds(text: string | null | undefined): string[] {
  const seen: string[] = [];
  for (const match of (text ?? "").matchAll(HANDLE_RE)) if (!seen.includes(match[1])) seen.push(match[1]);
  return seen;
}

// The host's lists of what a message carried, written for the model (daedalus/host/attached_files.py
// reads the same two shapes back). An orchestrator's: a header, then `- att:<id> <name> (…)` per file
// or `- <name>: not kept — …`. Everyone else's: `Attached files:`, then `- <path> (<mime>, <n> bytes)`
// per file, where the path may hold spaces, braces and parentheses of its own (a Windows paste is
// `{GUID}.png`). The bubble shows the operator's own words and the files are drawn above it, so the
// list is not printed twice.
const KEPT_HEADER_RE = /^Attached files \(kept by handle[^\n]*\):\s*$/;
const KEPT_LINE_RE = /^- (att:[0-9a-f]{12} .*|[^\n]+: not kept — .*)$/;
const PLAIN_HEADER_RE = /^Attached files:\s*$/;
const PLAIN_LINE_RE = /^- .+? \([\w.+-]+\/[\w.+-]+(?:;[^,()]*)?, \d+ bytes\).*$/;
/** What an upload says when the operator typed nothing; with the list gone it is not their words either. */
const STAND_INS = new Set(["File attached.", "Files attached."]);

/** How many lines from `at` make one item of the plain list: an item a wrapping editor or transport broke
 *  in two is joined back, as the host does. Zero when the line is not an item. */
function plainItem(lines: string[], at: number): number {
  if (!lines[at]?.startsWith("- ")) return 0;
  let line = lines[at];
  for (let end = at + 1; ; end++) {
    if (PLAIN_LINE_RE.test(line)) return end - at;
    if (end >= lines.length || lines[end].startsWith("- ") || !lines[end].trim() || line.length > 4096) return 0;
    line = `${line} ${lines[end].trim()}`;
  }
}

/** The operator's text without the host's list of the files it carried; anything else is left. The last
 *  list is the host's — it is appended after the operator's words, which could quote one. */
export function withoutAttachedList(text: string): string {
  const lines = text.split("\n");
  let at = lines.length - 1;
  while (at >= 0 && !KEPT_HEADER_RE.test(lines[at]) && !PLAIN_HEADER_RE.test(lines[at])) at--;
  if (at < 0) return text;
  const kept: string[] = [];
  let end = at + 1;
  if (KEPT_HEADER_RE.test(lines[at])) {
    while (end < lines.length && KEPT_LINE_RE.test(lines[end])) {
      if (!lines[end].startsWith("- att:")) kept.push(lines[end]);
      end++;
    }
  } else {
    for (let n = plainItem(lines, end); n > 0; n = plainItem(lines, end)) end += n;
    // A header with no item the host would have written is somebody's words, not the list.
    if (end === at + 1) return text;
  }
  const head = [...lines.slice(0, at), ...kept];
  const tail = lines.slice(end);
  // The list sat between two paragraphs; without it their blank lines would meet as one wide gap.
  while (head.length && !head[head.length - 1].trim() && tail.length && !tail[0].trim()) tail.shift();
  const words = [...head, ...tail].join("\n").trim();
  return STAND_INS.has(words) ? "" : words;
}

/** The query that names the files of some ids, or null when there are none. */
export function filesKey(ids: string[]): string | null {
  return ids.length ? `/api/files?ids=${ids.join(",")}` : null;
}

/** Where a kept file's bytes are served, in the shape the preview and the download links take. */
export function keptBase(id: string): string {
  return `/api/files/${id}`;
}
