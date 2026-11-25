// A unified diff read into files, hunks and lines with their numbers on both sides — what a `.patch`
// holds, what `git diff` prints, what a receipt's output sometimes is. The viewer colours the rows.

export type DiffLine = { type: "add" | "del" | "ctx" | "meta"; text: string; oldNo: number | null; newNo: number | null };
export type DiffHunk = { header: string; lines: DiffLine[] };
/** What happened to a file: its header says it was created, removed or moved, or it only changed inside. */
export type DiffFileKind = "modified" | "added" | "deleted" | "renamed";
/** `binary` is a file git would not print lines for; it has a header and no hunks. */
export type DiffFile = { oldPath: string; newPath: string; hunks: DiffHunk[]; added: number; removed: number; binary: boolean; kind: DiffFileKind };

const HUNK_RE = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$/;

/** Whether a text is a diff at all: a header, or a hunk mark with added and removed lines after it. */
export function looksLikeDiff(text: string): boolean {
  const head = text.slice(0, 4000);
  if (/^diff --git /m.test(head)) return true;
  if (/^--- .*\n\+\+\+ /m.test(head)) return true;
  return /^@@ -\d+(,\d+)? \+\d+(,\d+)? @@/m.test(head);
}

function stripPrefix(p: string): string {
  const s = p.trim().split("\t")[0];
  return s === "/dev/null" ? s : s.replace(/^[ab]\//, "");
}

/** The files a diff touches, each with its hunks; a diff with no file header is one unnamed file. */
export function parseDiff(text: string): DiffFile[] {
  const files: DiffFile[] = [];
  let file: DiffFile | null = null;
  let hunk: DiffHunk | null = null;
  let oldNo = 0;
  let newNo = 0;
  let oldLeft = 0;
  let newLeft = 0;
  const startFile = (oldPath: string, newPath: string) => {
    file = { oldPath, newPath, hunks: [], added: 0, removed: 0, binary: false, kind: "modified" };
    files.push(file);
    hunk = null;
  };
  for (const raw of text.split("\n")) {
    const line = raw.endsWith("\r") ? raw.slice(0, -1) : raw;
    if (line.startsWith("diff --git ")) {
      const m = /^diff --git a\/(.*?) b\/(.*)$/.exec(line);
      startFile(m?.[1] ?? "", m?.[2] ?? "");
      continue;
    }
    if (line.startsWith("--- ") && (!hunk || (oldLeft === 0 && newLeft === 0))) {
      hunk = null;
      const previous = file as DiffFile | null;
      if (!previous || previous.hunks.length) startFile(stripPrefix(line.slice(4)), "");
      else previous.oldPath = stripPrefix(line.slice(4));
      continue;
    }
    if (line.startsWith("+++ ") && !hunk) {
      if (!file) startFile("", "");
      file!.newPath = stripPrefix(line.slice(4));
      continue;
    }
    const h = HUNK_RE.exec(line);
    if (h) {
      if (!file) startFile("", "");
      oldNo = Number(h[1]);
      newNo = Number(h[3]);
      oldLeft = Number(h[2] ?? 1);
      newLeft = Number(h[4] ?? 1);
      hunk = { header: line, lines: [] };
      file!.hunks.push(hunk);
      continue;
    }
    if (!hunk) {
      // The extended header lines say what a file with no hunks was: made, removed, moved or binary.
      const header = file as DiffFile | null;
      if (header) {
        if (line.startsWith("new file mode")) header.kind = "added";
        else if (line.startsWith("deleted file mode")) header.kind = "deleted";
        else if (line.startsWith("rename from ") || line.startsWith("rename to ")) header.kind = "renamed";
        else if (line.startsWith("Binary files ") || line.startsWith("GIT binary patch")) header.binary = true;
      }
      continue;
    }
    if (line.startsWith("+")) {
      hunk.lines.push({ type: "add", text: line.slice(1), oldNo: null, newNo: newNo++ });
      file!.added++;
      newLeft--;
    } else if (line.startsWith("-")) {
      hunk.lines.push({ type: "del", text: line.slice(1), oldNo: oldNo++, newNo: null });
      file!.removed++;
      oldLeft--;
    } else if (line.startsWith("\\")) {
      hunk.lines.push({ type: "meta", text: line.slice(1).trim(), oldNo: null, newNo: null });
    } else if (line.startsWith(" ")) {
      hunk.lines.push({ type: "ctx", text: line.slice(1), oldNo: oldNo++, newNo: newNo++ });
      oldLeft--;
      newLeft--;
    } else {
      // Something that is not a diff line ends the hunk: a trailing sentence in a receipt.
      hunk = null;
    }
  }
  for (const f of files) {
    // A patch without git headers says the same through /dev/null.
    if (f.kind === "modified" && f.oldPath === "/dev/null") f.kind = "added";
    else if (f.kind === "modified" && f.newPath === "/dev/null") f.kind = "deleted";
    else if (f.kind === "modified" && f.oldPath && f.newPath && f.oldPath !== f.newPath) f.kind = "renamed";
  }
  return files;
}

/** The name a file row shows: the new path, or the old one for a deletion. */
export function diffFileName(f: DiffFile): string {
  if (f.newPath && f.newPath !== "/dev/null") return f.newPath;
  if (f.oldPath && f.oldPath !== "/dev/null") return f.oldPath;
  return "";
}

/**
 * Where a review note on the clicked row lands: the file at the branch head and a line in it.
 *
 * Notes are pinned to the reviewed commit, so they speak in new-file lines. A removed line has no
 * number there; it is pinned to the line the removal sits before (or after, at the end of a hunk),
 * which is where the reader of the new file would look for it. A deleted file has no head path and
 * a "\ No newline" mark is no line at all, so both give nothing and the typed fields remain.
 */
export function lineAnchor(file: DiffFile, hunk: DiffHunk, index: number): { path: string; line: number } | null {
  const path = file.newPath && file.newPath !== "/dev/null" ? file.newPath : "";
  const row = hunk.lines[index];
  if (!path || !row || row.type === "meta") return null;
  if (row.newNo != null) return { path, line: row.newNo };
  for (let i = index + 1; i < hunk.lines.length; i++) if (hunk.lines[i].newNo != null) return { path, line: hunk.lines[i].newNo! };
  for (let i = index - 1; i >= 0; i--) if (hunk.lines[i].newNo != null) return { path, line: hunk.lines[i].newNo! };
  const start = Number(HUNK_RE.exec(hunk.header)?.[3] ?? 1);
  return { path, line: Math.max(1, start) };
}

/** What a unified view draws, top to bottom: a hunk's mark, a line, a run of unchanged lines folded
 *  away (`fold`, which the diff holds and the reader can open) or left out of the patch (`gap`, which
 *  only the file itself could show). */
export type DiffRow =
  | { type: "hunk"; header: string }
  | { type: "line"; line: DiffLine }
  | { type: "fold"; id: string; lines: DiffLine[] }
  | { type: "gap"; count: number };

/**
 * A file's rows with long runs of unchanged lines folded to their edges.
 *
 * A run longer than `min` keeps `keep` lines beside the change and hides the rest behind one row, so a
 * patch made with a wide context reads as its changes. A run at the start of a hunk keeps only the end
 * that touches a change, and one at its end only the start. `open` holds the ids of the folds the
 * reader has opened; an id is the hunk and the index of the run's first line, stable while the file is.
 */
export function diffRows(file: DiffFile, open: ReadonlySet<string> = new Set(), keep = 3, min = 8): DiffRow[] {
  const rows: DiffRow[] = [];
  let reached = 0;
  file.hunks.forEach((hunk, h) => {
    // Lines of the old file between this hunk and the one before it, which the patch does not carry.
    const span = HUNK_RE.exec(hunk.header);
    if (span) {
      const first = Number(span[1]) + (Number(span[2] ?? 1) === 0 ? 1 : 0);
      if (h > 0 && first - reached > 0) rows.push({ type: "gap", count: first - reached });
      reached = Number(span[1]) + (Number(span[2] ?? 1) === 0 ? 1 : Number(span[2] ?? 1));
    }
    rows.push({ type: "hunk", header: hunk.header });
    const lines = hunk.lines;
    let i = 0;
    while (i < lines.length) {
      if (lines[i].type !== "ctx") {
        rows.push({ type: "line", line: lines[i++] });
        continue;
      }
      let end = i;
      while (end < lines.length && lines[end].type === "ctx") end++;
      const id = `${h}:${i}`;
      const head = i === 0 ? 0 : keep;
      const tail = end === lines.length ? 0 : keep;
      if (end - i > min && !open.has(id) && end - i - head - tail > 0) {
        for (let k = i; k < i + head; k++) rows.push({ type: "line", line: lines[k] });
        rows.push({ type: "fold", id, lines: lines.slice(i + head, end - tail) });
        for (let k = end - tail; k < end; k++) rows.push({ type: "line", line: lines[k] });
      } else {
        for (let k = i; k < end; k++) rows.push({ type: "line", line: lines[k] });
      }
      i = end;
    }
  });
  return rows;
}

/** One row of the side-by-side view: the old line and the new one that replaces it, either missing. */
export type SplitRow =
  | { type: "hunk"; header: string }
  | { type: "fold"; id: string; lines: DiffLine[] }
  | { type: "gap"; count: number }
  | { type: "pair"; left: DiffLine | null; right: DiffLine | null };

/**
 * The same rows laid side by side: an unchanged line stands on both sides, and a run of removed
 * lines is paired in order with the run of added lines that follows it, the shorter side left empty.
 * A "no newline" mark is no line of either file and is left out.
 */
export function splitRows(rows: DiffRow[]): SplitRow[] {
  const out: SplitRow[] = [];
  let removed: DiffLine[] = [];
  let added: DiffLine[] = [];
  const flush = () => {
    for (let k = 0; k < Math.max(removed.length, added.length); k++) out.push({ type: "pair", left: removed[k] ?? null, right: added[k] ?? null });
    removed = [];
    added = [];
  };
  for (const row of rows) {
    if (row.type === "line") {
      const line = row.line;
      if (line.type === "del") removed.push(line);
      else if (line.type === "add") added.push(line);
      else if (line.type === "ctx") {
        flush();
        out.push({ type: "pair", left: line, right: line });
      }
      continue;
    }
    flush();
    out.push(row);
  }
  flush();
  return out;
}

/** The directory part of a path with its trailing slash, and the file's own name. */
export function splitPath(path: string): { dir: string; name: string } {
  const at = path.lastIndexOf("/");
  return at < 0 ? { dir: "", name: path } : { dir: path.slice(0, at + 1), name: path.slice(at + 1) };
}
