// The explorer's tree as a value, with nothing of the browser in it: which folders are loaded, which
// are open, what the rows in view are, where the keyboard goes next, what a search asks the host for
// and what it makes of the answer. The component in explorer.tsx fetches and draws; this file decides.

export type Entry = { name: string; dir: boolean; size: number; mtime: number; ignored?: boolean };

/** What the tree knows about one folder: its listing once it arrived, whether it is open. */
export type Folder = { entries: Entry[] | null; open: boolean; loading: boolean; error: string | null };

/** Folders by path; "" is the root. A folder that was never expanded is not here. */
export type Tree = Record<string, Folder>;

export type Row = {
  path: string;
  name: string;
  dir: boolean;
  size: number;
  mtime: number;
  depth: number;
  open: boolean;
  loading: boolean;
  error: string | null;
  /** A dotted name: shown only on request. */
  hidden: boolean;
  /** A folder nobody reads by hand (node_modules, a venv, a cache): dimmed, closed until asked. */
  ignored: boolean;
};

/** Folders the search on the host skips too; the tree shows them dimmed and closed. */
export const IGNORED = new Set([".git", "__pycache__", "node_modules", ".venv", "venv", ".checkpoints", ".mypy_cache", ".ruff_cache", ".pytest_cache", ".cache", "dist", "build", ".next", "target"]);

export const isHidden = (name: string): boolean => name.startsWith(".");
export const isIgnored = (name: string): boolean => IGNORED.has(name);

export function emptyTree(): Tree {
  return { "": { entries: null, open: true, loading: false, error: null } };
}

export function parentOf(path: string): string {
  const i = path.lastIndexOf("/");
  return i < 0 ? "" : path.slice(0, i);
}

export function join(dir: string, name: string): string {
  return dir ? `${dir}/${name}` : name;
}

/** Folders first, then by name, the way a person expects a listing to read. */
export function sortEntries(entries: Entry[]): Entry[] {
  return [...entries].sort((a, b) => (a.dir === b.dir ? a.name.localeCompare(b.name, undefined, { sensitivity: "base", numeric: true }) : a.dir ? -1 : 1));
}

function folder(tree: Tree, path: string): Folder {
  return Object.hasOwn(tree, path) ? tree[path] : { entries: null, open: false, loading: false, error: null };
}

export function setLoading(tree: Tree, path: string): Tree {
  return { ...tree, [path]: { ...folder(tree, path), loading: true, error: null } };
}

export function setListing(tree: Tree, path: string, entries: Entry[]): Tree {
  return { ...tree, [path]: { ...folder(tree, path), entries: sortEntries(entries), loading: false, error: null } };
}

export function setError(tree: Tree, path: string, error: string): Tree {
  return { ...tree, [path]: { ...folder(tree, path), loading: false, error } };
}

/** Open or close a folder. Opening one that was never listed asks for the listing (`load` is true). */
export function toggleFolder(tree: Tree, path: string): { tree: Tree; load: boolean } {
  const f = folder(tree, path);
  if (f.open) return { tree: { ...tree, [path]: { ...f, open: false } }, load: false };
  return { tree: { ...tree, [path]: { ...f, open: true } }, load: f.entries === null && !f.loading };
}

export function openFolder(tree: Tree, path: string): { tree: Tree; load: boolean } {
  const f = folder(tree, path);
  if (f.open) return { tree, load: false };
  return toggleFolder(tree, path);
}

/** Every folder ancestor of a path, root excluded: `a/b/c.txt` → `["a", "a/b"]`. */
export function ancestorsOf(path: string): string[] {
  const parts = path.split("/").filter(Boolean);
  const out: string[] = [];
  for (let i = 1; i < parts.length; i++) out.push(parts.slice(0, i).join("/"));
  return out;
}

/** The folders currently open and listed — what a refresh re-reads. */
export function openFolders(tree: Tree): string[] {
  return Object.keys(tree).filter((p) => tree[p].open && tree[p].entries !== null);
}

/** Forget every listing; the open state stays, so a refresh re-reads what was in view. */
export function forgetListings(tree: Tree): Tree {
  const out: Tree = {};
  for (const [p, f] of Object.entries(tree)) out[p] = { ...f, entries: null, error: null };
  return out;
}

/** The rows in view, top to bottom: open folders flattened, dotted names only when asked for. */
export function visibleRows(tree: Tree, opts: { showHidden: boolean }): Row[] {
  const out: Row[] = [];
  const walk = (dir: string, depth: number) => {
    const f = tree[dir];
    if (!f || !f.open || !f.entries) return;
    for (const e of f.entries) {
      const hidden = isHidden(e.name);
      if ((hidden || isIgnored(e.name) || e.ignored) && !opts.showHidden) continue;
      const path = join(dir, e.name);
      const sub = e.dir ? folder(tree, path) : null;
      out.push({ path, name: e.name, dir: e.dir, size: e.size, mtime: e.mtime, depth, open: !!sub?.open, loading: !!sub?.loading, error: sub?.error ?? null, hidden, ignored: !!e.ignored || isIgnored(e.name) });
      if (e.dir) walk(path, depth + 1);
    }
  };
  walk("", 0);
  return out;
}

/** How many dotted entries the open folders hold — what the toggle says it can show. */
export function hiddenCount(tree: Tree): number {
  let n = 0;
  for (const f of Object.values(tree)) if (f.open && f.entries) n += f.entries.filter((e) => isHidden(e.name)).length;
  return n;
}


export type KeyAction = { index: number; do: "open" | "expand" | "collapse" | null };

/**
 * Where a key takes the focus in a list of rows, and what it does there. Down and Up move; Right
 * opens a closed folder or steps into an open one; Left closes an open folder or goes to the parent;
 * Enter opens a file or toggles a folder; Backspace goes to the parent; Home and End are the ends.
 */
export function keyMove(rows: Row[], index: number, key: string): KeyAction | null {
  const n = rows.length;
  if (!n) return null;
  const i = Math.max(0, Math.min(n - 1, index));
  const row = rows[i];
  const parentIndex = () => {
    const parent = parentOf(row.path);
    const j = rows.findIndex((r) => r.path === parent);
    return j;
  };
  switch (key) {
    case "ArrowDown":
      return { index: Math.min(n - 1, i + 1), do: null };
    case "ArrowUp":
      return { index: Math.max(0, i - 1), do: null };
    case "Home":
      return { index: 0, do: null };
    case "End":
      return { index: n - 1, do: null };
    case "ArrowRight":
      if (!row.dir) return { index: i, do: null };
      if (!row.open) return { index: i, do: "expand" };
      return { index: rows[i + 1]?.depth > row.depth ? i + 1 : i, do: null };
    case "ArrowLeft": {
      if (row.dir && row.open) return { index: i, do: "collapse" };
      const j = parentIndex();
      return { index: j >= 0 ? j : i, do: null };
    }
    case "Backspace": {
      const j = parentIndex();
      return j >= 0 ? { index: j, do: null } : { index: 0, do: null };
    }
    case "Enter":
    case " ":
      return { index: i, do: row.dir ? (row.open ? "collapse" : "expand") : "open" };
    default:
      return null;
  }
}


export type SearchMode = "name" | "content";

export type NameHit = { path: string; kind: "file" | "dir"; size: number; mtime: number };
export type GrepHit = { path: string; line: number; text: string };

/** What the host answers a name search with. */
export type NameSearch = { query: string; results: NameHit[]; truncated: boolean; engine?: string };
/** What the host answers a content search with. */
export type GrepSearch = { query: string; hits: GrepHit[]; truncated: boolean };

export function isGlob(q: string): boolean {
  return /[*?[]/.test(q);
}

/** A glob over one name or a whole path: `*` and `**` cross nothing and everything respectively. */
export function globToRegExp(glob: string): RegExp {
  let re = "";
  for (let i = 0; i < glob.length; i++) {
    const c = glob[i];
    if (c === "*") {
      if (glob[i + 1] === "*") {
        re += glob[i + 2] === "/" ? "(?:.*/)?" : ".*";
        i++;
        if (glob[i + 1] === "/") i++;
      } else re += "[^/]*";
    } else if (c === "?") re += "[^/]";
    else if (c === "[") {
      const end = glob.indexOf("]", i);
      if (end > i) {
        re += glob.slice(i, end + 1);
        i = end;
      } else re += "\\[";
    } else re += c.replace(/[.+^${}()|\\]/g, "\\$&");
  }
  try { return new RegExp(`^${re}$`, "i"); }
  catch { return new RegExp(`^${glob.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}$`, "i"); }
}

/** The host's rule, applied to what is loaded: a substring of the name, or a glob over the name and the path. */
export function matchesName(q: string, path: string): boolean {
  const name = path.split("/").pop() ?? path;
  if (!q) return false;
  if (isGlob(q)) {
    const re = globToRegExp(q);
    return re.test(name) || re.test(path);
  }
  return name.toLowerCase().includes(q.toLowerCase());
}

/** A name search over the rows the tree has loaded — what the panel does when the host has no search. */
export function filterLoaded(rows: Row[], q: string): NameHit[] {
  return rows.filter((r) => matchesName(q, r.path)).map((r) => ({ path: r.path, kind: r.dir ? "dir" : "file", size: r.size, mtime: r.mtime }));
}

/** A host's answer, shaped: anything that is not the contract reads as "no search here". */
export function readNameSearch(body: unknown): NameSearch | null {
  if (!body || typeof body !== "object" || !Array.isArray((body as NameSearch).results)) return null;
  const b = body as NameSearch;
  return { query: String(b.query ?? ""), results: b.results.filter((r) => r && typeof r.path === "string").map((r) => ({ path: r.path, kind: r.kind === "dir" ? "dir" : "file", size: Number(r.size ?? 0), mtime: Number(r.mtime ?? 0) })), truncated: !!b.truncated, engine: b.engine };
}

export function readGrep(body: unknown): GrepSearch | null {
  if (!body || typeof body !== "object" || !Array.isArray((body as GrepSearch).hits)) return null;
  const b = body as GrepSearch;
  return { query: String(b.query ?? ""), hits: b.hits.filter((h) => h && typeof h.path === "string").map((h) => ({ path: h.path, line: Number(h.line ?? 1), text: String(h.text ?? "") })), truncated: !!b.truncated };
}


export const TREE_W = 224;
export const TREE_W_MIN = 160;
export const TREE_W_MAX = 480;
/** A panel this wide keeps the tree beside the viewer. */
export const SPLIT_MIN = 720;

/** Search includes cached children of closed folders, without making those folders open on screen. */
export function loadedRows(tree: Tree, showHidden: boolean): Row[] {
  return visibleRows(Object.fromEntries(Object.entries(tree).map(([p, f]) => [p, { ...f, open: true }])), { showHidden });
}

export function typeAhead(rows: Row[], index: number, text: string): number {
  for (let step = 1; step <= rows.length; step++) {
    const i = (Math.max(-1, index) + step) % rows.length;
    if (rows[i].name.toLocaleLowerCase().startsWith(text.toLocaleLowerCase())) return i;
  }
  return index;
}

/** Every folder closed but the root, listings kept: Collapse all is a view change, not a reload. */
export function collapseAll(tree: Tree): Tree {
  return Object.fromEntries(Object.entries(tree).map(([p, f]) => [p, p === "" || !f.open ? f : { ...f, open: false }]));
}

/**
 * A path cut before its last part, so the header can elide the middle and keep the folder's own name
 * readable: `/srv/workspaces/s_example` → `["/srv/workspaces/", "s_example"]`. A trailing separator
 * is dropped first, or the last part would be empty and the name the reader looks for would vanish.
 */
export function splitPath(path: string): [string, string] {
  const trimmed = path.length > 1 ? path.replace(/[\\/]+$/, "") : path;
  const cut = Math.max(trimmed.lastIndexOf("/"), trimmed.lastIndexOf("\\"));
  return cut < 0 || cut === trimmed.length - 1 ? ["", trimmed] : [trimmed.slice(0, cut + 1), trimmed.slice(cut + 1)];
}

/** One content hit's file, with its lines under it in the order the host found them. */
export type GrepGroup = { path: string; hits: GrepHit[] };

export function groupGrep(hits: GrepHit[]): GrepGroup[] {
  const groups = new Map<string, GrepGroup>();
  for (const hit of hits) {
    const group = groups.get(hit.path) ?? { path: hit.path, hits: [] };
    group.hits.push(hit);
    groups.set(hit.path, group);
  }
  return [...groups.values()];
}

/**
 * A found line, split where the query matches, for highlighting. The line is trimmed and, when the
 * first match sits far along it, started a little before the match behind an ellipsis: the snippet is
 * one line clipped at the panel's edge, and a match past the clip showed a line with nothing found in it.
 * The match is literal and ignores case; a query that does not occur leaves the line unmarked.
 */
export function markMatches(line: string, query: string, lead = 24): { text: string; hit: boolean }[] {
  let text = line.trim();
  const q = query.trim().toLocaleLowerCase();
  if (!q) return [{ text, hit: false }];
  const first = text.toLocaleLowerCase().indexOf(q);
  if (first > lead + 8) text = `…${text.slice(first - lead)}`;
  const lower = text.toLocaleLowerCase();
  const out: { text: string; hit: boolean }[] = [];
  let at = 0;
  for (let i = lower.indexOf(q); i >= 0; i = lower.indexOf(q, i + q.length)) {
    if (i > at) out.push({ text: text.slice(at, i), hit: false });
    out.push({ text: text.slice(i, i + q.length), hit: true });
    at = i + q.length;
  }
  if (at < text.length) out.push({ text: text.slice(at), hit: false });
  return out;
}
