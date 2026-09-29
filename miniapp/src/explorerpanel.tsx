// Listings stay with the mounted explorer when the reader switches to Preview. Opening a folder
// is the only thing that reads its children; searching never recursively downloads the workspace.

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, SessionFolder } from "./api";
import { Switch, copyText } from "./components";
import { OverflowMenu } from "./dialogs";
import { fileIcon } from "./artifact";
import { folderBase, folderName } from "./folders";
import { Icon } from "./icons";
import { PreviewSource, downloadHref } from "./preview";
import { errorText, fmtBytes } from "./ui";
import { plural, t } from "./i18n";
import { Entry, GrepHit, NameHit, Row, SearchMode, Tree, ancestorsOf, emptyTree, filterLoaded, keyMove, loadedRows, openFolder, openFolders, parentOf, readGrep, readNameSearch, setError, setListing, setLoading, toggleFolder, typeAhead, visibleRows } from "./explorer";

import { FileSkeleton } from "./feedback";

/**
 * A folder's files. `base` is the session's own address; with more than one entry in `folders` the
 * pane offers a switch between them, and `home` is the one `base` already means.
 */
export function Explorer({ base: sessionBase, root, upload: canUpload = false, folders = [], home = "", onPreview, toast, refresh = 0, written = [] }: { base: string; root?: string; upload?: boolean; folders?: SessionFolder[]; home?: string; onPreview: (src: PreviewSource) => void; toast?: (text: string) => void; refresh?: number; written?: string[] }) {
  const [folder, setFolder] = useState(home);
  const shown = folders.find((f) => f.id === folder);
  const base = folderBase(sessionBase, folder, home);
  const athome = !folder || folder === home;
  // The pane writes only where the agent could: a folder the session may not write offers no upload.
  const uploadUrl = canUpload && (shown ? shown.writable : athome) ? `${base}/files/upload` : undefined;
  const [tree, setTree] = useState<Tree>(emptyTree);
  const current = useRef(tree);
  current.current = tree;
  const [selected, setSelected] = useState("");
  const [selectedLine, setSelectedLine] = useState<number | undefined>();
  const [showHidden, setShowHidden] = useState(false);
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<SearchMode>("name");
  const [search, setSearch] = useState<{ hits: (NameHit | GrepHit)[]; busy: boolean; note: string }>({ hits: [], busy: false, note: "" });
  const [uploading, setUploading] = useState(false);
  const [generation, setGeneration] = useState(0);
  const list = useRef<HTMLDivElement>(null);
  const upload = useRef<HTMLInputElement>(null);
  const requests = useRef(new Map<string, AbortController>());
  const prefix = useRef({ text: "", at: 0 });

  const load = useCallback(async (path: string) => {
    requests.current.get(path)?.abort();
    const controller = new AbortController();
    requests.current.set(path, controller);
    setTree((s) => setLoading(s, path));
    try {
      const res = await fetch(`${base}/files?path=${encodeURIComponent(path)}`, { headers: api.authHeaders(), signal: controller.signal });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      const data = await res.json() as { kind: string; entries: Entry[] };
      if (data.kind !== "dir" || !Array.isArray(data.entries)) throw new Error(t("explorer.notfolder"));
      if (!controller.signal.aborted) setTree((s) => setListing(s, path, data.entries));
    } catch (e) {
      if (!controller.signal.aborted) setTree((s) => setError(s, path, errorText(e)));
    } finally {
      if (requests.current.get(path) === controller) requests.current.delete(path);
    }
  }, [base]);
  useEffect(() => {
    setTree(emptyTree());
    setSelected("");
    void load("");
    const pending = requests.current;
    return () => { for (const c of pending.values()) c.abort(); pending.clear(); };
  }, [load]);
  useEffect(() => {
    const paths = openFolders(current.current);
    setTree((s) => Object.fromEntries(Object.entries(s).map(([p, f]) => [p, f.open ? f : { ...f, entries: null }])));
    for (const path of paths) void load(path);
  }, [refresh, generation, load]);

  useEffect(() => {
    if (!query.trim()) { setSearch({ hits: [], busy: false, note: "" }); return; }
    let gone = false;
    setSearch({ hits: [], busy: true, note: "" });
    const timer = window.setTimeout(async () => {
      try {
        const data = await api.get<unknown>(`${base}/files/${mode === "name" ? "search" : "grep"}?q=${encodeURIComponent(query.trim())}&limit=200`);
        const result = mode === "name" ? readNameSearch(data) : readGrep(data);
        if (!result) throw new ApiError(404, "");
        if (!gone) setSearch({ hits: "results" in result ? result.results : result.hits, busy: false, note: result.truncated ? t("explorer.truncated") : "" });
      } catch (e) {
        if (gone) return;
        if (e instanceof ApiError && e.status === 404) setSearch({ hits: filterLoaded(loadedRows(current.current, showHidden), query.trim()), busy: false, note: t(mode === "name" ? "explorer.local" : "explorer.local.content") });
        else if (e instanceof ApiError && e.status === 501) setSearch({ hits: [], busy: false, note: t("explorer.nogrep") });
        else setSearch({ hits: [], busy: false, note: errorText(e) });
      }
    }, 220);
    return () => { gone = true; window.clearTimeout(timer); };
  }, [base, query, mode, showHidden, refresh, generation]);

  const rows: (Row & { line?: number; snippet?: string })[] = query.trim() ? search.hits.map((h) => ({ path: h.path, name: h.path, dir: "kind" in h && h.kind === "dir", size: "size" in h ? h.size : 0, mtime: 0, depth: 0, open: false, loading: false, error: null, hidden: false, ignored: false, ...("line" in h ? { line: h.line, snippet: h.text } : {}) })) : visibleRows(tree, { showHidden });
  const index = Math.max(0, rows.findIndex((r) => r.path === selected && r.line === selectedLine));
  const active = rows[index];
  const folderPath = selected ? active?.dir ? active.path : parentOf(selected) : "";
  const crumbs = folderPath.split("/").filter(Boolean);
  function toggle(row: Row) {
    const next = toggleFolder(current.current, row.path);
    setTree(next.tree);
    if (next.load) void load(row.path);
  }
  function open(row: typeof rows[number]) {
    setSelected(row.path);
    setSelectedLine(row.line);
    if (row.dir) {
      if (query) { setQuery(""); void reveal(row.path); }
      else toggle(row);
    } else onPreview({ base, path: row.path, ...(row.line ? { lines: String(row.line) } : {}) });
  }
  async function reveal(path: string) {
    setQuery("");
    for (const dir of [...ancestorsOf(path), path]) {
      setTree((s) => openFolder(s, dir).tree);
      if (!current.current[dir]?.entries) await load(dir);
    }
    setSelected(path);
  }
  function focusAt(i: number) {
    if (!rows[i]) return;
    setSelected(rows[i].path);
    setSelectedLine(rows[i].line);
    list.current?.querySelectorAll<HTMLElement>("[role=treeitem]")[i]?.focus();
  }
  function onKey(e: React.KeyboardEvent) {
    if ((e.target as HTMLElement).closest(".explorer-actions")) return;
    const move = keyMove(rows, index, e.key);
    if (move) {
      e.preventDefault();
      focusAt(move.index);
      if (move.do === "open") open(rows[move.index]);
      else if (move.do) toggle(rows[move.index]);
    } else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
      e.preventDefault();
      prefix.current = { text: Date.now() - prefix.current.at < 700 ? prefix.current.text + e.key : e.key, at: Date.now() };
      focusAt(typeAhead(rows, index, prefix.current.text));
    }
  }
  async function send(files: FileList | null) {
    if (!files?.length || !uploadUrl) return;
    setUploading(true);
    try {
      const form = new FormData();
      form.append("path", folderPath);
      for (const file of Array.from(files)) form.append("files", file, file.name);
      const res = await fetch(uploadUrl, { method: "POST", headers: api.authHeaders(), body: form });
      if (!res.ok) throw new Error(res.statusText);
      const result = await res.json() as { files: string[] };
      toast?.(plural("session.files.added", result.files.length));
      setGeneration((n) => n + 1);
    } catch (e) { toast?.(errorText(e)); }
    finally { setUploading(false); if (upload.current) upload.current.value = ""; }
  }
  return (
    <div className="files explorer">
      {folders.length > 1 && <div className="explorer-folders">
        <select className="field" aria-label={t("explorer.folder")} value={folder || home} onChange={(e) => { setFolder(e.target.value); setQuery(""); setSelected(""); }}>
          {folders.map((f) => <option key={f.id} value={f.id}>{folderName(f)}</option>)}
        </select>
        {shown && <span className="chip" title={shown.path}>{t(shown.env === "host" ? "explorer.env.host" : "explorer.env.container")}</span>}
        {shown && !shown.writable && <span className="chip" title={t("explorer.folder.readonly.title")}><Icon name="lock" size={14} />{t("project.readonly.short")}</span>}
      </div>}
      <div className="explorer-search">
        <input className="field" type="search" aria-label={t("explorer.search")} placeholder={t("explorer.search")} value={query} onChange={(e) => setQuery(e.target.value)} />
        <select className="field" aria-label={t("explorer.mode")} value={mode} onChange={(e) => setMode(e.target.value as SearchMode)}><option value="name">{t("explorer.names")}</option><option value="content">{t("explorer.contents")}</option></select>
      </div>
      <div className="crumbs explorer-crumbs">
        <button className="crumb" onClick={() => { setQuery(""); setSelected(""); list.current?.scrollTo(0, 0); }}>{(!athome && shown ? folderName(shown) : root) || t("session.files.crumb")}</button>
        {crumbs.map((c, i) => <span key={i}> › <button className="crumb" onClick={() => void reveal(crumbs.slice(0, i + 1).join("/"))}>{c}</button></span>)}
      </div>
      <div className="explorer-tools">
        {/* A switch with its words, on a row ruled off from the tree: as a bare grey line of text
            straight above the files it read as a heading over them, not as a control. */}
        <label className="explorer-hidden">
          <Switch checked={showHidden} onChange={setShowHidden} label={t("explorer.hidden")} />
          <span>{t("explorer.hidden")}</span>
        </label>
        <button className="iconbtn small" aria-label={t("panel.reload")} onClick={() => { if (tree[""]?.error) void load(""); setGeneration((n) => n + 1); }}><Icon name="reload" size={14} /></button>
        {uploadUrl && <><input ref={upload} type="file" multiple hidden onChange={(e) => void send(e.target.files)} /><button className="iconbtn small" disabled={uploading} aria-label={t("session.files.upload")} onClick={() => upload.current?.click()}><Icon name="up" size={14} /></button></>}
      </div>
      {uploading && <div className="preview-progress busy" role="progressbar" aria-label={t("session.files.uploading")}><i /></div>}
      {tree[""]?.error && <div className="empty">{tree[""].error}</div>}
      {query && search.note && <div className="explorer-note" role="status">{search.note}</div>}
      {(query ? search.busy : !tree[""]?.entries && tree[""]?.loading) && <FileSkeleton />}
      <div className="explorer-tree" role="tree" aria-label={t("panel.tab.files")} ref={list} onKeyDown={onKey}>
        {rows.map((row, i) => <div key={`${row.path}:${row.line ?? ""}`} role="treeitem" aria-level={row.depth + 1} aria-expanded={row.dir ? row.open : undefined} aria-selected={i === index} aria-busy={row.loading} tabIndex={i === index ? 0 : -1} className={`filerow tree-row ${row.hidden || row.ignored ? "dim" : ""} ${i === index ? "selected" : ""} ${row.snippet !== undefined ? "grep-row" : ""}`} data-path={row.path} style={{ paddingLeft: 8 + row.depth * 16 }} onFocus={() => { setSelected(row.path); setSelectedLine(row.line); }} onClick={() => open(row)} onContextMenu={(e) => { e.preventDefault(); e.currentTarget.querySelector<HTMLButtonElement>(".explorer-actions button")?.click(); }}>
          <span className="tree-chevron" aria-hidden>{row.dir ? row.loading ? "…" : row.open ? "⌄" : "›" : ""}</span>
          <Icon name={row.dir ? "folder" : fileIcon(row.name)} size={16} />
          <span className="grow truncate"><span className="title">{row.name}{row.line ? `:${row.line}` : ""}</span>{row.snippet !== undefined && <span className="grep-snippet">{row.snippet}</span>}</span>
          {athome && written.includes(row.path) && <span className="written-badge" title={t("explorer.written")} aria-label={t("explorer.written")}>●</span>}
          {!row.dir && <span className="file-size">{fmtBytes(row.size)}</span>}
          <span className="explorer-actions"><OverflowMenu small items={row.dir ? [{ label: t("panel.tab.files"), icon: "folder", onSelect: () => open(row) }, { label: t("explorer.copy"), icon: "copy", onSelect: () => void copyText(row.path).then((ok) => toast?.(t(ok ? "common.copied" : "svc.copyfail"))) }] : [
            { label: t("preview.open"), icon: "eye", onSelect: () => open(row) },
            { label: t("explorer.copy"), icon: "copy", onSelect: () => void copyText(row.path).then((ok) => toast?.(t(ok ? "common.copied" : "svc.copyfail"))) },
            { label: t("common.download"), icon: "download", onSelect: () => { const a = document.createElement("a"); a.href = downloadHref(base, row.path); a.download = row.path.split("/").pop()!; a.click(); } },
            { label: t("panel.opennew"), icon: "external", onSelect: () => { window.open(downloadHref(base, row.path), "_blank", "noopener,noreferrer"); } },
          ]} /></span>
          {row.error && <span className="tree-error" title={row.error}>!</span>}
        </div>)}
      </div>
      {!rows.length && !search.busy && tree[""]?.entries && <div className="empty">{t("session.files.empty")}</div>}
    </div>
  );
}
