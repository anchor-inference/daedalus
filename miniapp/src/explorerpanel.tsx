// Listings stay with the mounted explorer when the reader switches to Preview. Opening a folder
// is the only thing that reads its children; searching never recursively downloads the workspace.

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, SessionFolder } from "./api";
import { copyText, useCopied } from "./ui/components";
import { MenuItem, OverflowMenu } from "./ui/dialogs";
import { fileIcon } from "./artifact";
import { folderBase, folderName } from "./folders";
import { EnvPill } from "./envpill";
import { Icon } from "./icons";
import { PreviewSource, downloadHref } from "./preview";
import { errorText, fmtBytes } from "./ui";
import { plural, t } from "./i18n";
import { Entry, GrepHit, NameHit, Row, SearchMode, Tree, ancestorsOf, collapseAll, emptyTree, filterLoaded, groupGrep, keyMove, loadedRows, markMatches, openFolder, openFolders, parentOf, readGrep, readNameSearch, setError, setListing, setLoading, splitPath, toggleFolder, typeAhead, visibleRows } from "./explorer";

import { FileSkeleton } from "./feedback";
import "./explorerpanel.css";

/** A row as the pane draws it: a tree entry, a name hit, a content hit's file (`count`) or one of its lines. */
type ViewRow = Row & { line?: number; snippet?: string; count?: number };

/**
 * A folder's files. `base` is the session's own address; with more than one entry in `folders` the
 * pane offers a switch between them, and `home` is the one `base` already means. The header names the
 * folder by its real path — `rootPath` for the home folder, a listed folder's own `path` otherwise —
 * because a session's or a project's name is a title in any language, not a place on a disk.
 * `onOpenBeside`, when given, adds "Open beside" to a file's menu: the host opens it as a tab of its own.
 */
export function Explorer({ base: sessionBase, rootPath, env, upload: canUpload = false, folders = [], home = "", onPreview, onOpenBeside, toast, refresh = 0, written = [] }: { base: string; rootPath?: string; env?: "container" | "host"; upload?: boolean; folders?: SessionFolder[]; home?: string; onPreview: (src: PreviewSource) => void; onOpenBeside?: (src: PreviewSource) => void; toast?: (text: string) => void; refresh?: number; written?: string[] }) {
  const [folder, setFolder] = useState(home);
  const shown = folders.find((f) => f.id === (folder || home));
  const base = folderBase(sessionBase, folder, home);
  const athome = !folder || folder === home;
  // The pane writes only where the agent could: a folder the session may not write offers no upload.
  const uploadUrl = canUpload && (shown ? shown.writable : athome) ? `${base}/files/upload` : undefined;
  const where = shown?.path || (athome ? rootPath ?? "" : "");
  const shownEnv = shown?.env ?? (athome ? env : undefined);
  const [copied, copy] = useCopied();
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

  const blank = { mtime: 0, open: false, loading: false, error: null, hidden: false, ignored: false };
  const grepHits = search.hits.filter((h): h is GrepHit => "line" in h);
  const matchFiles = groupGrep(grepHits);
  // A content search reads as the files it found, each with its count, and the lines under it; a name
  // search as the paths it found. Both are rows of the same tree, so the keyboard walks them alike.
  const rows: ViewRow[] = !query.trim() ? visibleRows(tree, { showHidden })
    : grepHits.length ? matchFiles.flatMap((g) => [
      { ...blank, path: g.path, name: g.path, dir: false, size: 0, depth: 0, count: g.hits.length },
      ...g.hits.map((h) => ({ ...blank, path: h.path, name: h.path, dir: false, size: 0, depth: 1, line: h.line, snippet: h.text })),
    ])
    : search.hits.map((h) => ({ ...blank, path: h.path, name: h.path, dir: "kind" in h && h.kind === "dir", size: "size" in h ? h.size : 0, depth: 0 }));
  const index = Math.max(0, rows.findIndex((r) => r.path === selected && r.line === selectedLine));
  const chosen = rows.find((r) => r.path === selected && r.line === selectedLine);
  const folderPath = chosen ? chosen.dir ? chosen.path : parentOf(chosen.path) : "";
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
    setSelectedLine(undefined);
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
      setGeneration((n) => n + 1);
    } catch (e) { toast?.(errorText(e)); }
    finally { setUploading(false); if (upload.current) upload.current.value = ""; }
  }
  function reload() {
    if (tree[""]?.error) void load("");
    setGeneration((n) => n + 1);
  }
  function pick(id: string) {
    setFolder(id);
    setQuery("");
    setSelected("");
  }
  const copyPath = async (path: string) => {
    const ok = await copyText(path);
    if (!ok) toast?.(t("svc.copyfail"));
    return ok;
  };
  async function copyWhere() {
    if (!where) return;
    if (!await copy(where, "where")) toast?.(t("svc.copyfail"));
  }
  const folderItems: MenuItem[] = folders.map((f) => ({
    label: folderName(f),
    icon: f.id === (folder || home) ? "check" : "folder",
    hint: f.path,
    aside: <><EnvPill env={f.env === "host" ? "host" : "container"} tiny />{!f.writable && <span className="chip explorer-readonly">{t("project.readonly.short")}</span>}</>,
    onSelect: () => pick(f.id),
  }));
  const [whereHead, whereTail] = splitPath(where);
  const writtenHere = (path: string) => athome && written.includes(path);
  const statusParts = chosen ? chosen.path.split("/") : [];
  return (
    <div className="files explorer">
      <div className="explorer-head">
        <Icon name="folder" size={14} />
        {/* The real path, its middle given up first on a narrow panel: the last part is the folder's
            own name, the one thing the reader checks. The whole path is in the title and is what a
            press copies, since the visible text may be cut. */}
        {where
          ? <button className="explorer-where" title={where} aria-label={t("explorer.where.copy", { path: where })} onClick={() => void copyWhere()}>
            <span className="explorer-where-head">{whereHead}</span><span className="explorer-where-tail">{whereTail}</span>
            {copied === "where" && <Icon name="check" size={12} />}
          </button>
          : <span className="explorer-where">{shown ? folderName(shown) : t("panel.tab.files")}</span>}
        {folders.length > 1 && <OverflowMenu className="explorer-switch" small icon="chevron" label={t("explorer.folder")} items={folderItems} />}
        {shownEnv && <EnvPill env={shownEnv === "host" ? "host" : "container"} tiny />}
        {shown && !shown.writable && <span className="chip explorer-readonly" title={t("explorer.folder.readonly.title")}><Icon name="lock" size={12} />{t("project.readonly.short")}</span>}
        <span className="grow" />
        {uploadUrl && <><input ref={upload} type="file" multiple hidden onChange={(e) => void send(e.target.files)} /><button className="iconbtn small" disabled={uploading} aria-label={t("session.files.upload")} title={t("explorer.upload.title")} onClick={() => upload.current?.click()}><Icon name="up" size={16} /></button></>}
        <OverflowMenu className="explorer-more" small label={t("explorer.tools")} items={[
          { label: t("explorer.hidden"), checked: showHidden, onSelect: () => setShowHidden((v) => !v) },
          { label: t("panel.reload"), icon: "reload", onSelect: reload },
          { label: t("explorer.collapse"), icon: "compact", onSelect: () => { setTree((s) => collapseAll(s)); setQuery(""); } },
          ...(folders.length > 1 ? ["-" as const, ...folderItems] : []),
        ]} />
      </div>
      {uploading && <div className="preview-progress busy" role="progressbar" aria-label={t("session.files.uploading")}><i /></div>}
      <div className={`explorer-filter ${query ? "filled" : ""}`}>
        <Icon name="search" size={14} />
        <input type="search" aria-label={t("explorer.filter")} placeholder={t("explorer.filter")} value={query} onChange={(e) => setQuery(e.target.value)} />
        <span className="explorer-mode" role="radiogroup" aria-label={t("explorer.mode")}>
          {(["name", "content"] as SearchMode[]).map((m) => <button key={m} type="button" role="radio" aria-checked={mode === m} className={mode === m ? "on" : ""} onClick={() => setMode(m)}>{t(m === "name" ? "explorer.mode.names" : "explorer.mode.text")}</button>)}
        </span>
      </div>
      {query.trim() && !search.busy && (grepHits.length > 0 || search.note) && <div className="explorer-note" role="status">
        {grepHits.length > 0 && <span>{plural("explorer.matches", grepHits.length, { files: plural("explorer.matches.files", matchFiles.length) })}</span>}
        {search.note && <span>{search.note}</span>}
      </div>}
      {tree[""]?.error && <div className="empty">{tree[""].error}</div>}
      {(query ? search.busy : !tree[""]?.entries && tree[""]?.loading) && <FileSkeleton />}
      <div className="explorer-tree" role="tree" aria-label={t("panel.tab.files")} ref={list} onKeyDown={onKey}>
        {rows.map((row, i) => {
          const line = row.snippet !== undefined;
          const items: MenuItem[] = row.dir ? [
            { label: t("common.open"), icon: "folder", onSelect: () => open(row) },
            { label: t("explorer.copy"), icon: "copy", ticks: true, onSelect: () => copyPath(row.path) },
          ] : [
            { label: t("preview.open"), icon: "eye", onSelect: () => open(row) },
            ...(onOpenBeside ? [{ label: t("explorer.beside"), icon: "columns" as const, onSelect: () => onOpenBeside({ base, path: row.path, ...(row.line ? { lines: String(row.line) } : {}) }) }] : []),
            { label: t("explorer.copy"), icon: "copy", ticks: true, onSelect: () => copyPath(row.path) },
            { label: t("common.download"), icon: "download", onSelect: () => { const a = document.createElement("a"); a.href = downloadHref(base, row.path); a.download = row.path.split("/").pop()!; a.click(); } },
            { label: t("panel.opennew"), icon: "external", onSelect: () => { window.open(downloadHref(base, row.path), "_blank", "noopener,noreferrer"); } },
          ];
          return <div key={`${row.path}:${row.line ?? ""}`} role="treeitem" aria-level={row.depth + 1} aria-expanded={row.dir ? row.open : row.count !== undefined ? true : undefined} aria-selected={i === index} aria-busy={row.loading} tabIndex={i === index ? 0 : -1} className={`filerow tree-row ${row.hidden || row.ignored ? "dim" : ""} ${chosen === row ? "selected" : ""} ${line ? "grep-row" : ""}`} data-path={row.path} data-line={row.line} style={{ paddingLeft: line ? 30 : 4 + row.depth * 16 }} onFocus={() => { setSelected(row.path); setSelectedLine(row.line); }} onClick={() => open(row)}>
            {line ? <>
              <span className="explorer-ln">{row.line}</span>
              <code className="grep-snippet">{markMatches(row.snippet!, query).map((part, k) => part.hit ? <mark key={k}>{part.text}</mark> : part.text)}</code>
            </> : <>
              <span className={`tree-chevron ${row.dir && row.open || row.count !== undefined ? "open" : ""}`} aria-hidden>{row.dir && (row.loading ? "…" : <Icon name="chevron" size={12} />)}</span>
              <Icon name={row.dir ? "folder" : fileIcon(row.name)} size={16} />
              <span className="truncate title">{row.name}</span>
              {writtenHere(row.path) && <span className="written-badge" title={t("explorer.written")} aria-label={t("explorer.written")} />}
              <span className="grow" />
              {row.count !== undefined && <span className="explorer-count">{row.count}</span>}
              {!row.dir && row.count === undefined && row.size > 0 && <span className="file-size">{fmtBytes(row.size)}</span>}
            </>}
            {!line && <span className="explorer-actions"><OverflowMenu contextSelector=".filerow" small items={items} /></span>}
            {row.error && <span className="tree-error" title={row.error}>!</span>}
          </div>;
        })}
      </div>
      {!rows.length && !search.busy && tree[""]?.entries && <div className="empty">{t("session.files.empty")}</div>}
      {chosen && <div className="explorer-status">
        {/* Where the selected file sits, each folder of it a way back to that folder in the tree: what
            the breadcrumbs above the tree did, moved under it so the tree starts at the top. */}
        <span className="explorer-status-path" title={chosen.path}>
          {statusParts.map((part, k) => <span key={k}>{k > 0 && "/"}{k < statusParts.length - 1
            ? <button type="button" onClick={() => void reveal(statusParts.slice(0, k + 1).join("/"))}>{part}</button>
            : <b>{part}{chosen.line ? `:${chosen.line}` : ""}</b>}</span>)}
        </span>
        {!chosen.dir && chosen.size > 0 && <span className="explorer-status-size">{fmtBytes(chosen.size)}</span>}
        {writtenHere(chosen.path) && <span className="explorer-status-written">{t("explorer.written.run")}</span>}
      </div>}
    </div>
  );
}
