// The file column of a review: the files grouped under their folders, narrowed by what is typed, and
// the order the "next file" button walks. Pure, so the page's few rules about it are held by tests.

import { diffFileName, splitPath, type DiffFile } from "./diff";

export type FolderGroup = { dir: string; files: DiffFile[] };

/** The files whose path holds `query`, grouped by folder in the order the patch lists them. */
export function groupByFolder(files: DiffFile[], query = ""): FolderGroup[] {
  const needle = query.trim().toLowerCase();
  const groups: FolderGroup[] = [];
  for (const file of files) {
    const path = diffFileName(file);
    if (needle && !path.toLowerCase().includes(needle)) continue;
    const { dir } = splitPath(path);
    const last = groups[groups.length - 1];
    const group = last && last.dir === dir ? last : groups.find((g) => g.dir === dir);
    if (group) group.files.push(file);
    else groups.push({ dir, files: [file] });
  }
  return groups;
}

/** The file to read after `current`: the first one not yet viewed, going on from it and wrapping round. */
export function nextToView(paths: string[], current: string, viewed: ReadonlySet<string>): string | null {
  const at = paths.indexOf(current);
  for (let step = 1; step < paths.length; step++) {
    const path = paths[(Math.max(at, 0) + step) % paths.length];
    if (!viewed.has(path)) return path;
  }
  return null;
}
