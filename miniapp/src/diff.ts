// A unified diff read into files, hunks and lines with their numbers on both sides — what a `.patch`
// holds, what `git diff` prints, what a receipt's output sometimes is. The viewer colours the rows.

export type DiffLine = { type: "add" | "del" | "ctx" | "meta"; text: string; oldNo: number | null; newNo: number | null };
export type DiffHunk = { header: string; lines: DiffLine[] };
export type DiffFile = { oldPath: string; newPath: string; hunks: DiffHunk[]; added: number; removed: number };

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
    file = { oldPath, newPath, hunks: [], added: 0, removed: 0 };
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
      if (file && line && !/^(index |new file|deleted file|similarity|rename |old mode|new mode|Binary files)/.test(line)) continue;
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
