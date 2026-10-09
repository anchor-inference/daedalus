// The desktop's left column in Agents mode: "New chat" as the one accent, the projects and the chats
// apart, and the chats by day. Which project is a project and which is a chat's own scratch folder is
// decided once, in grouping.ts (`isProject`), by the same rule the phone's Chats page splits by.
//
// A chat row is two lines: the title takes the whole width, and the line under it says the state in
// words only when it asks for the operator (waiting, working, failed), then the host mark, the model or
// the loop's cadence, and the time. A project row is one line with a square tile, where a chat has a
// round plate: the shape says which is which before the name is read. Projects start collapsed, the
// one holding the open chat opens itself, and a collapsed project still shows its live chats, because
// a closed folder must never hide a question.
//
// The keyboard: `/` reaches the search from anywhere outside a text field, the arrows walk the rows
// (left and right fold and unfold a project), Enter opens, Ctrl+Enter opens beside, F2 renames, E
// archives and Delete deletes after the same confirmation the menu asks for.

import { useContextActions } from "../ui/context-menu";
import { type ReactNode, memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, Project, SessionList, SessionSummary, Settings } from "../api";
import { agentFolders, folderName, offersFolderChoice, projectPath, projectReachable } from "../folders";
import { Skeleton, ToolPicker, fmtInterval, statusWord } from "../ui/components";
import { type MenuItem, OverflowMenu, Sheet, confirmDialog, toast } from "../ui/dialogs";
import { relTime, shortModel, untilShort } from "../format";
import { type Folder, type Row as RowModel, type SidebarDay, agentName, arrange, folderOpen, kindOf, liveRow, rememberFolder, sidebarSections } from "../grouping";
import { Icon, type IconName } from "../icons";
import { MoveSessionSheet, ProjectSettingsSheet, useProjects } from "../projects";
import { openNewProject } from "../project/NewProject";
import { navigate, parse, pathFor, sessionPath } from "../router";
import { agentsListing } from "../mode";
import { invalidate, useQuery } from "../store";
import { useStreamUp } from "../events";
import { WindowedRows } from "../virtual";
import { errorText } from "../ui";
import { latinKey } from "../navigation";
import { insideTerminal } from "../terminal/keys";
import { plural, t } from "../i18n";
import { useReveal } from "../reveal";
import { HostMark, RunEnv, useRunOn } from "../runon";

type SearchList = SessionList & { semantic: boolean; reason: string; partial: boolean; indexing: boolean };
type View = "all" | "attention" | "working" | "loops" | "archive";

/** How many chats an open project lists before "N more": enough for the work in hand, few enough
 *  that the "Chats" section below stays on screen with six projects open above it. */
const KIDS_SHOWN = 6;

/** Whether a key press belongs to a text field, where letters and Delete are the field's own. */
function typing(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  return !!el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT" || el.isContentEditable);
}

export function SessionsScreen({ onOpen, toast, current, project = "", projects = [], onProjects }: { onOpen: (id: string) => void; toast: (t: string) => void; current?: string; project?: string; projects?: Project[]; onProjects?: () => void }) {
  const [view, setView] = useState<View>("all");
  // Loops is a cut of the whole list the server has no view for; the chip filters it here.
  const serverView = view === "loops" ? "all" : view;
  const listUrl = `/api/sessions?view=${serverView}`;
  // A run starting, finishing or asking arrives as an event while the stream is up, and the list is
  // read again then; the poll every minute is only the net under it.
  const live = useStreamUp();
  const { data, error, loading } = useQuery<SessionList>(listUrl, { pollMs: live ? 60000 : 5000, staleMs: 3000 });
  // The chips count over the whole list whichever chip is on, so a number never changes under the
  // pointer that is about to press it. The same address as the list under "All", so one request.
  const everything = useQuery<SessionList>("/api/sessions?view=all", { pollMs: live ? 60000 : 5000, staleMs: 3000 });
  const [extra, setExtra] = useState<SessionSummary[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [paging, setPaging] = useState(false);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchList | null>(null);
  const [pending, setPending] = useState(false);
  const [searchError, setSearchError] = useState("");
  const searchRef = useRef<HTMLInputElement>(null);
  const searching = !!query.trim();
  useEffect(() => {
    setExtra([]);
    setNext(data?.next_cursor ?? null);
  }, [data, view]);
  useEffect(() => {
    setResults(null);
    setSearchError("");
    setPending(searching);
    if (!searching) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void fetch(`/api/sessions/search?q=${encodeURIComponent(query.trim())}&project=${encodeURIComponent(project)}`, { headers: api.authHeaders(), signal: controller.signal })
        .then(async (response) => {
          if (!response.ok) throw new Error(t("search.failed"));
          const answer: SearchList = await response.json();
          if (!controller.signal.aborted) setResults(answer);
        })
        .catch(() => { if (!controller.signal.aborted) setSearchError(t("search.failed")); })
        .finally(() => { if (!controller.signal.aborted) setPending(false); });
    }, 250);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [query, project, searching]);
  // `/` reaches the search from anywhere a key is not already somebody's: Ctrl+K is the palette's,
  // and a focused terminal or text field keeps every letter. Heard on the window, after the
  // document: a screen with a search of its own (the calendar's `/`) takes the key there first and
  // marks it handled, and the column, mounted before that screen, used to steal it.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey || e.defaultPrevented) return;
      if (typing(e.target) || insideTerminal(e.target)) return;
      if (document.querySelector("[role=dialog], [role=menu]")) return;
      e.preventDefault();
      searchRef.current?.focus();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const lens = projects.find((p) => p.id === project);

  // The Agents list never shows what belongs to orchestration mode, found by a search or not.
  const raw = searching ? results : data ? { ...data, sessions: [...data.sessions, ...extra], next_cursor: next } : data;
  const listing = useMemo(() => (raw ? agentsListing(raw) : raw), [raw]);
  const sessions = listing?.sessions ?? [];
  const arranged = useMemo(
    () => arrange(sessions, listing?.projects ?? [], { project, results: searching, filter: view === "loops" ? "loops" : "all" }),
    [listing, project, searching, view],
  );
  const filtered = searching || view !== "all";
  const sections = useMemo(() => sidebarSections(arranged.folders, { filtered }), [arranged, filtered]);
  const counts = useMemo(() => {
    const top = (everything.data ? agentsListing(everything.data).sessions : []).filter((s) => !s.metadata?.subagent_of && (!project || s.project_id === project));
    return {
      all: top.length,
      attention: top.filter((s) => s.needs_attention || s.status === "waiting").length,
      working: top.filter((s) => kindOf(s, false) === "working").length,
    };
  }, [everything.data, project]);

  const list = useRef<HTMLDivElement>(null);
  const onListKey = useCallback((e: React.KeyboardEvent) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp" && e.key !== "Home" && e.key !== "End") return;
    if (typing(e.target) || e.ctrlKey || e.metaKey || e.altKey) return;
    const rows = Array.from(list.current?.querySelectorAll<HTMLElement>("[data-nav]") ?? []);
    if (!rows.length) return;
    const at = rows.findIndex((row) => row === e.target);
    const to = e.key === "Home" ? 0 : e.key === "End" ? rows.length - 1 : at < 0 ? 0 : Math.max(0, Math.min(rows.length - 1, at + (e.key === "ArrowDown" ? 1 : -1)));
    e.preventDefault();
    rows[to].focus();
  }, []);

  const chip = (name: View, label: string, count?: number, tone = "") => (
    <button type="button" className={`sb-chip ${tone} ${view === name ? "on" : ""}`} aria-pressed={view === name} onClick={() => setView(view === name && name !== "all" ? "all" : name)}>
      {label}{count !== undefined && <b className="num">{count}</b>}
    </button>
  );
  const moreOn = view === "loops" || view === "archive";
  const projectCount = sections.projects.length;

  return (
    <>
      <div className="sb-tools">
        <button type="button" className="sb-new" onClick={() => navigate(pathFor("agents"))} title={t("side.newchat.title")}>
          <Icon name="compose" size={16} />
          <span>{t("ph.newchat")}</span>
          <kbd>{t("side.newchat.keys")}</kbd>
        </button>
        <div className="sb-pbtns">
          {onProjects && (
            <button type="button" className="project-chip sb-pbtn" onClick={onProjects} aria-haspopup="dialog" title={lens ? projectPath(lens) : t("side.allprojects.title")}>
              <Icon name={lens ? "folder" : "grid"} size={14} />
              <span className="truncate">{lens ? lens.name : t("shell.projects.all")}</span>
            </button>
          )}
          <button type="button" className="sb-pbtn sb-newproject" onClick={() => openNewProject()}>
            <Icon name="plus" size={15} />
            <span className="truncate">{t("ph.newproject")}</span>
          </button>
        </div>
        <label className={`sb-search ${query ? "filled" : ""}`}>
          <Icon name="search" size={15} />
          <input ref={searchRef} type="search" placeholder={t("side.search.placeholder")} value={query} maxLength={500} aria-label={t("search.label")}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") { if (query) setQuery(""); else e.currentTarget.blur(); }
              if (e.key === "ArrowDown") { e.preventDefault(); list.current?.querySelector<HTMLElement>("[data-nav]")?.focus(); }
            }} />
          {query
            ? <button type="button" className="sb-clear" onClick={() => { setQuery(""); searchRef.current?.focus(); }} aria-label={t("ph.search.clear")} title={t("ph.search.clear")}><Icon name="close" size={14} /></button>
            : <kbd title={t("side.search.key")}>/</kbd>}
        </label>
        {!searching && (
          <div className="sb-chips" role="group" aria-label={t("agents.filter.label")}>
            {chip("all", t("agents.filter.all"), everything.data ? counts.all : undefined)}
            {chip("attention", t("side.filter.attention"), everything.data ? counts.attention : undefined, "warn")}
            {chip("working", t("side.filter.working"), everything.data ? counts.working : undefined)}
            {moreOn && chip(view, view === "loops" ? t("ph.filter.loops") : t("agents.filter.archive"))}
            <OverflowMenu small className={`sb-chip icon ${moreOn ? "on" : ""}`} label={t("side.filter.more")} items={[
              { label: t("ph.filter.loops"), icon: "loop", checked: view === "loops", onSelect: () => setView(view === "loops" ? "all" : "loops") },
              { label: t("agents.filter.archive"), icon: "archive", checked: view === "archive", onSelect: () => setView(view === "archive" ? "all" : "archive") },
            ]} />
          </div>
        )}
      </div>
      <div ref={list} className="screen agents-screen sb-list" onKeyDown={onListKey}>
        {searching && <div className="search-notice sb-notice" role="status">
          <Icon name="search" size={13} />
          <span>
            {pending ? t("search.loading") : searchError || (results?.semantic ? t("search.semantic") : t("search.exact"))}
            {!pending && !searchError && results && !results.semantic && <> <a href="/app/settings/components">{t("search.enable")}</a></>}
            {results?.indexing && <span> {t("search.indexing")}</span>}
            {results?.partial && <span> {t("search.partial")}</span>}
          </span>
        </div>}
        {loading && !error && !data && <Skeleton rows={5} />}
        {error && !data && <div className="empty"><b>{t("agents.error")}</b><div>{error}</div></div>}
        {data && !searching && view === "all" && arranged.total === 0 && projectCount === 0 && (
          <div className="empty">
            <b>{lens ? t("agents.empty.project", { name: lens.name }) : t("agents.empty")}</b>
            <div>{lens ? t("agents.empty.project.sub", { root: projectPath(lens) }) : t("agents.empty.sub")}</div>
          </div>
        )}
        {((results && !pending && arranged.shown === 0) || (!searching && data && filtered && arranged.shown === 0)) && <div className="empty">{t("common.nothing")}</div>}
        {projectCount > 0 && (
          <>
            <div className="sb-sec first"><span>{t("side.projects")}</span><span className="n num">{projectCount}</span></div>
            {sections.projects.map((f) => (
              <ProjectGroup key={f.key} folder={f} onOpen={onOpen} current={current} filtered={filtered} toast={toast} />
            ))}
          </>
        )}
        {(sections.chats > 0 || view === "archive") && (
          <>
            <div className={`sb-sec ${projectCount > 0 ? "" : "first"}`}>
              <span>{view === "archive" ? t("agents.filter.archive") : t("side.chats")}</span><span className="n num">{sections.chats}</span>
              {!searching && (
                <span className="sb-sec-acts">
                  <button type="button" className={`iconbtn small quiet ${view === "archive" ? "on" : ""}`} aria-pressed={view === "archive"} onClick={() => setView(view === "archive" ? "all" : "archive")}
                    aria-label={t(view === "archive" ? "side.archive.hide" : "side.archive.show")} title={t(view === "archive" ? "side.archive.hide" : "side.archive.show")}>
                    <Icon name="archive" size={14} />
                  </button>
                </span>
              )}
            </div>
            {sections.days.map((d) => <DayGroup key={d.day} day={d.day} rows={d.rows} onOpen={onOpen} current={current} />)}
          </>
        )}
        {!searching && next && <button className="btn ghost load-more" disabled={paging} onClick={async () => {
          setPaging(true);
          try {
            const page = await api.get<SessionList>(`/api/sessions?view=${serverView}&cursor=${encodeURIComponent(next)}`);
            setExtra((held) => [...held, ...page.sessions.filter((row) => !data?.sessions.some((first) => first.id === row.id) && !held.some((old) => old.id === row.id))]);
            setNext(page.next_cursor ?? null);
          } catch (e) { toast(errorText(e)); }
          finally { setPaging(false); }
        }}>{t(paging ? "agents.loading" : "agents.more")}</button>}
      </div>
    </>
  );
}

/** One row per line a list draws: a chat, then the forks taken from it, then theirs. The nesting is
 *  in the model and not in the DOM, so the rows can be windowed as one list. */
type Line = { row: RowModel; fork?: { of: string; seq: number } };

function lines(rows: RowModel[], fork?: { of: string; seq: number }): Line[] {
  const out: Line[] = [];
  for (const r of rows) {
    out.push({ row: r, fork });
    for (const f of r.forks) out.push(...lines([f], { of: r.s.title, seq: f.s.metadata!.forked_from!.seq }));
  }
  return out;
}

/** Whether the open session is in these rows, a fork of one of them, or a subagent of one. */
function holds(rows: RowModel[], id: string | undefined): boolean {
  if (!id) return false;
  return rows.some((r) => r.s.id === id || r.kids.some((k) => k.id === id) || holds(r.forks, id));
}

/** The live state a project's tile carries: a question first, then work. */
function folderState(rows: RowModel[]): "waiting" | "running" | "" {
  const all = lines(rows).flatMap((l) => [l.row.s, ...l.row.kids]);
  if (all.some((s) => s.status === "waiting")) return "waiting";
  if (all.some((s) => s.status === "running" || s.status === "compacting")) return "running";
  return "";
}

/** One day of the chats section. Windowed: a long history is the same few rows in the DOM. */
function DayGroup({ day, rows, onOpen, current }: { day: SidebarDay; rows: RowModel[]; onOpen: (id: string) => void; current?: string }) {
  const host = useRef<HTMLDivElement>(null);
  const shown = useMemo(() => lines(rows), [rows]);
  const keys = useMemo(() => shown.map((l) => l.row.s.id), [shown]);
  return (
    <div ref={host} className="sb-day" data-day={day}>
      <div className="sb-dayhead">{t(`side.day.${day}`)}</div>
      <WindowedRows keys={keys} host={host} rowSelector=":scope > .sb-row" estimate={44} render={(i) => (
        <ChatRow row={shown[i].row} fork={shown[i].fork} onOpen={onOpen} current={current} projectName={shown[i].row.s.project} />
      )} />
    </div>
  );
}

type GroupProps = { folder: Folder; onOpen: (id: string) => void; current?: string; filtered: boolean; toast: (text: string) => void };

/** Whether a project's group would look the same. The listing is re-fetched whole and every object in
 *  it is new each time; `sig` is built once while the folders are arranged, so this costs a string
 *  comparison where a reconcile of every row would cost the frame. */
function sameGroup(a: GroupProps, b: GroupProps): boolean {
  const l = a.folder, r = b.folder;
  if (a.filtered !== b.filtered || a.current !== b.current || a.onOpen !== b.onOpen || a.toast !== b.toast) return false;
  if (l.key !== r.key || l.name !== r.name || l.total !== r.total || l.system !== r.system || l.last_message_at !== r.last_message_at) return false;
  if (projectPath(l.project) !== projectPath(r.project)) return false;
  return l.sig === r.sig;
}

/**
 * A project: a one-line row with a square tile and its count, and its chats on a guide line under it
 * once it is open. Collapsed is the default and is remembered per project; the project holding the
 * open chat opens itself without remembering it, and a search or a filter opens what it found.
 * Collapsed, it still lists its live chats (waiting, working, failed).
 */
const ProjectGroup = memo(function ProjectGroup({ folder, onOpen, current, filtered, toast }: GroupProps) {
  const [open, setOpen] = useState(() => folderOpen(folder.key));
  const [all, setAll] = useState(false);
  const [editing, setEditing] = useState(false);
  const [adding, setAdding] = useState(false);
  const section = useRef<HTMLElement | null>(null);
  const head = useRef<HTMLDivElement | null>(null);
  const mine = holds(folder.rows, current);
  const showing = open || mine || (filtered && folder.rows.length > 0);
  const toggle = useCallback((to?: boolean) => {
    const value = to ?? !showing;
    setOpen(value);
    rememberFolder(folder.key, value);
  }, [folder.key, showing]);
  const editProject = useCallback(() => setEditing(true), []);
  const newIn = useCallback(() => setAdding(true), []);
  const revealer = useReveal();
  const items: MenuItem[] = [
    { label: t("side.project.new", { name: folder.name }), icon: "compose", onSelect: newIn },
    ...(folder.system ? [{ label: t("agents.folder.tovoice"), icon: "mic" as const, onSelect: () => navigate(pathFor("voice")) }] : []),
    { label: t("project.settings.for", { name: folder.name }), icon: "settings", onSelect: editProject },
    ...(revealer ? [{ label: revealer.label, icon: "external" as const, onSelect: () => void revealer.reveal({ project_id: folder.key }) }] : []),
    { label: t(showing ? "agents.folder.hide" : "agents.folder.show", { name: folder.name }), icon: "chevron", onSelect: () => toggle() },
  ];
  useContextActions(head, items);
  const all_lines = useMemo(() => lines(folder.rows), [folder.rows]);
  // Only the live lines themselves: an idle fork of a working chat is not what the collapsed row
  // keeps in sight for.
  const peek = useMemo(() => all_lines.filter((l) => liveRow(l.row)), [all_lines]);
  const shown = showing ? (all ? all_lines : all_lines.slice(0, KIDS_SHOWN)) : peek;
  const keys = useMemo(() => shown.map((l) => l.row.s.id), [shown]);
  const hidden = showing ? all_lines.length - shown.length : 0;
  const state = folderState(folder.rows);
  const onKey = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.target !== e.currentTarget) return;
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
    else if (e.key === "ArrowRight") {
      e.preventDefault();
      if (!showing) toggle(true);
      else section.current?.querySelector<HTMLElement>(".sb-kids [data-nav]")?.focus();
    } else if (e.key === "ArrowLeft" && showing) { e.preventDefault(); toggle(false); }
  };
  return (
    <section ref={section} data-project={folder.key} className={`sb-project ${showing ? "open" : ""} ${folder.system ? "system" : ""}`}>
      <div ref={head} className="sb-row sb-prow" data-nav="project" data-project-head="" role="button" tabIndex={0} aria-expanded={showing}
        aria-label={t(showing ? "agents.folder.hide" : "agents.folder.show", { name: folder.name })} title={projectPath(folder.project)}
        onClick={() => toggle()} onKeyDown={onKey}>
        <span className="sb-tile"><Icon name={folder.system ? "mic" : "folder"} size={14} />{state && <span className={`sb-st ${state}`} />}</span>
        <span className="sb-main"><span className="sb-t">{folder.name}</span></span>
        <span className="sb-ptrail">
          {showing ? <span className="sb-chev" aria-hidden><Icon name="chevron" size={14} /></span> : <span className="sb-pcount num" title={plural("agents.count", folder.total)}>{folder.total}</span>}
        </span>
        <span className="sb-acts" onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
          <button type="button" className="iconbtn small quiet sb-padd" onClick={newIn} aria-label={t("side.project.new", { name: folder.name })} title={t("side.project.new", { name: folder.name })}><Icon name="plus" size={16} /></button>
          <OverflowMenu small className="quiet" label={t("side.project.menu", { name: folder.name })} items={items} />
        </span>
      </div>
      {(shown.length > 0 || (showing && folder.rows.length === 0)) && (
        <div className={`sb-kids ${showing ? "" : "peek"}`}>
          {showing && folder.rows.length === 0 && <div className="sb-empty sub">{filtered ? t("common.nothing") : t("agents.folder.none")}</div>}
          <WindowedRows keys={keys} host={section} rowSelector=":scope > .sb-kids > .sb-row" estimate={40} render={(i) => (
            <ChatRow row={shown[i].row} fork={shown[i].fork} nested onOpen={onOpen} current={current} projectName={folder.name} system={folder.system} onProject={editProject} onNewIn={newIn} onParent={() => head.current?.focus()} />
          )} />
          {(hidden > 0 || (all && all_lines.length > KIDS_SHOWN)) && (
            <button type="button" className="sb-more" onClick={() => setAll((v) => !v)}>
              <Icon name="more" size={14} />{hidden > 0 ? t("side.more", { n: hidden }) : t("side.fewer")}
            </button>
          )}
        </div>
      )}
      {editing && <ProjectSettingsSheet project={{ ...folder.project, sessions: folder.rows.map((r) => ({ id: r.s.id, title: r.s.title })) }} onClose={() => setEditing(false)} onRemoved={() => setEditing(false)} toast={toast} />}
      {adding && <NewAgentSheet project={folder.key} onClose={() => setAdding(false)} onCreated={onOpen} toast={toast} />}
    </section>
  );
}, sameGroup);

/** "every 40m · run #4 · next in 12m", or the reason it is paused. */
function loopLine(s: SessionSummary): string {
  const loop = s.metadata?.loop;
  if (!loop) return "";
  const cadence = loop.mode === "interval" ? t("loop.every", { t: fmtInterval(loop.interval_seconds) }) : t("loop.selfpaced");
  const runs = `${t("agents.loop.runs", { n: loop.run_count })}${loop.max_runs ? `/${loop.max_runs}` : ""}`;
  if (loop.status === "active") return `${t("agents.loop.line", { cadence, runs })}${loop.next_run_at ? t("agents.loop.next", { t: untilShort(loop.next_run_at) }) : ""}`;
  const why = loop.pause_note || loop.stop_reason || "";
  return `${t("agents.loop.stopped", { status: statusWord(loop.status).toLowerCase() })}${why ? `: ${why.slice(0, 80)}` : ""} · ${runs}`;
}

type RowProps = {
  row: RowModel;
  onOpen: (id: string) => void;
  current?: string;
  fork?: { of: string; seq: number };
  /** Hung from a project's tile, on its guide line: 40 px and a smaller plate. */
  nested?: boolean;
  projectName: string;
  /** The chat lives in the installation's own project, whose chats carry the mic. */
  system?: boolean;
  onProject?: () => void;
  onNewIn?: () => void;
  /** Where Left goes from a chat inside a project: to the project's row. */
  onParent?: () => void;
};

/** What a row actually draws, so a fresh read of the list does not reconcile a row that did not
 *  change. The children are in it because their state is the leader's; the unread mark because a
 *  run that finishes unseen changes nothing else a row shows. */
function sameRow(a: RowProps, b: RowProps): boolean {
  const l = a.row.s, r = b.row.s;
  if (l.id !== r.id || l.title !== r.title || l.status !== r.status || !!l.unread_result !== !!r.unread_result || l.last_message_at !== r.last_message_at || l.model !== r.model || l.env !== r.env || !!l.archived !== !!r.archived) return false;
  if ((l.terminals ?? 0) !== (r.terminals ?? 0) || l.match?.snippet !== r.match?.snippet || l.project_id !== r.project_id) return false;
  if (a.projectName !== b.projectName || a.onProject !== b.onProject || a.onNewIn !== b.onNewIn || a.onOpen !== b.onOpen) return false;
  if ((a.current === l.id || a.row.kids.some((k) => k.id === a.current)) !== (b.current === r.id || b.row.kids.some((k) => k.id === b.current))) return false;
  if (a.nested !== b.nested || a.system !== b.system || a.fork?.seq !== b.fork?.seq || a.fork?.of !== b.fork?.of) return false;
  if (JSON.stringify(l.metadata?.loop ?? null) !== JSON.stringify(r.metadata?.loop ?? null)) return false;
  const lk = a.row.kids, rk = b.row.kids;
  return lk.length === rk.length && lk.every((k, i) => k.id === rk[i].id && k.status === rk[i].status && k.title === rk[i].title);
}

/** The state a plate's dot and the meta line's word say: only what asks for the operator or works. */
function rowState(s: SessionSummary, kids: SessionSummary[]): "waiting" | "running" | "failed" | "" {
  if (s.status === "waiting" || kids.some((k) => k.status === "waiting")) return "waiting";
  if (s.status === "failed") return "failed";
  if (s.status === "running" || s.status === "compacting" || kids.some((k) => k.status === "running")) return "running";
  return "";
}

/**
 * A chat: a round plate with its kind and a dot for its state, the title over the whole width, and a
 * meta line under it. Hovering or focusing it shows "Open beside" and the menu; the right click opens
 * the same menu, with the commands of the phone's sheet.
 */
const ChatRow = memo(function ChatRow({ row, onOpen, current, fork, nested, projectName, system, onProject, onNewIn, onParent }: RowProps) {
  const s = row.s;
  const kids = row.kids;
  const { items, layers, run } = useSessionCommands(s, { onProject, projectName });
  const here = current === s.id || kids.some((k) => k.id === current);
  const unread = !!s.unread_result;
  const state = rowState(s, kids);
  const icon: IconName = s.archived ? "archive" : s.metadata?.loop ? "loop" : fork || s.metadata?.forked_from ? "fork" : system ? "mic" : "bots";
  const open = () => onOpen(s.id);
  const beside = () => {
    const route = parse();
    if (route.session && route.session !== s.id) navigate(sessionPath(route.session, s.id));
    else open();
  };
  const menu: MenuItem[] = [
    { label: t("side.beside"), icon: "columns", hint: t("side.key.beside"), onSelect: beside },
    ...(onNewIn ? [{ label: t("side.project.new", { name: projectName }), icon: "compose" as const, onSelect: onNewIn }] : []),
    ...kids.map((kid) => ({ label: t("agents.sub.open", { name: agentName(kid) }), icon: "bots" as const, hint: statusWord(kid.status), onSelect: () => onOpen(kid.id) })),
    "-",
    ...items.map((item): MenuItem => {
      if (item === "-") return item;
      const hint = item.icon === "pen" ? t("side.key.rename") : item.icon === "archive" ? t("side.key.archive") : item.icon === "trash" ? t("side.key.delete") : undefined;
      return hint ? { ...item, hint } : item;
    }),
  ];
  const withRule = menu.flatMap((item, i) => (item !== "-" && item.danger && menu[i - 1] !== "-" ? ["-" as const, item] : [item]));
  const onKey = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.target !== e.currentTarget) return;
    const plain = !e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey;
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); beside(); }
    else if ((e.key === "Enter" || e.key === " ") && plain) { e.preventDefault(); open(); }
    else if (e.key === "F2" && plain) { e.preventDefault(); run.rename(); }
    else if (latinKey(e) === "e" && plain) { e.preventDefault(); void run.archive(); }
    else if (e.key === "Delete" && plain) { e.preventDefault(); void run.remove(); }
    else if (e.key === "ArrowLeft" && plain && onParent) { e.preventDefault(); onParent(); }
  };
  const tail = [
    s.metadata?.loop ? loopLine(s) : "",
    fork ? t("agents.fork.at", { n: fork.seq }) : "",
    s.model ? shortModel(s.model, 28) : "",
    kids.length ? plural("ph.subagents", kids.length) : "",
    s.workspace_own && !fork ? t("agents.own.chip") : "",
    s.metadata?.subagent_of ? t("agents.orphan") : "",
  ].filter(Boolean).join(" · ");
  return (
    <div data-session={s.id} data-nav="chat" className={`sb-row sb-chat ${here ? "current" : ""} ${unread ? "unread" : ""} ${nested ? "nested" : ""} ${fork ? "fork" : ""}`}
      title={s.model ? `${s.model} · ${s.id}` : s.id} role="link" aria-current={here ? "page" : undefined} tabIndex={0} onClick={open} onKeyDown={onKey}>
      <span className="sb-plate"><Icon name={icon} size={nested ? 12 : 14} />{state && <span className={`sb-st ${state}`} />}</span>
      <span className="sb-main">
        <span className="sb-l1">
          <span className="sb-t">{agentName(s)}</span>
          {unread && <span className="unread-dot" role="img" aria-label={t("agents.unread")} title={t("agents.unread")} />}
        </span>
        <span className="sb-l2">
          {s.match ? <span className="sb-m"><span className="search-passage sb-ell">{s.match.snippet}</span></span> : (
            <span className={`sb-m ${s.metadata?.loop?.status === "paused" ? "paused" : ""}`}>
              {state && <span className={`sb-word ${state}`}>{statusWord(state)}</span>}
              <HostMark env={s.env} />
              {s.terminals ? <span className="sb-terms num" title={plural("term.count", s.terminals)} aria-label={plural("term.count", s.terminals)}><Icon name="terminal" size={11} />{s.terminals}</span> : null}
              {tail && <span className="sb-ell">{tail}</span>}
            </span>
          )}
          <span className="sb-time num" title={new Date(s.last_message_at).toLocaleString()}>{relTime(s.last_message_at)}</span>
        </span>
      </span>
      <span className="sb-acts session-row-menu" onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
        <button type="button" className="iconbtn small quiet" onClick={beside} aria-label={t("side.beside")} title={t("side.beside")}><Icon name="columns" size={15} /></button>
        <OverflowMenu contextSelector="[data-session]" small className="quiet" label={`${s.title}: ${t("dlg.menu")}`} items={withRule} />
        {layers}
      </span>
    </div>
  );
}, sameRow);

/**
 * A session's commands, for every place that offers them: the desktop row's ⋮ menu and the phone's
 * long-press sheet list the same items and open the same sheets, so a command added here reaches both.
 * `layers` are the sheets the commands open, to be rendered beside whatever shows the items.
 */
export function useSessionCommands(session: SessionSummary, opts: { onProject?: () => void; projectName?: string } = {}): { items: MenuItem[]; layers: ReactNode; run: { rename: () => void; archive: () => Promise<void>; remove: () => Promise<void> } } {
  const [editing, setEditing] = useState(false);
  const [moving, setMoving] = useState(false);
  const [title, setTitle] = useState(session.title);
  const [saving, setSaving] = useState(false);
  async function rename() {
    if (!title.trim() || saving) return;
    setSaving(true);
    try {
      await api.patch(`/api/sessions/${session.id}`, { title: title.trim() });
      invalidate("/api/sessions");
      setEditing(false);
    } catch (error) { toast(errorText(error)); }
    finally { setSaving(false); }
  }
  async function remove() {
    if (!(await confirmDialog({ title: t("session.delete.title"), body: t("session.delete.body"), action: t("session.delete.action"), danger: true }))) return;
    try {
      await api.delete(`/api/sessions/${session.id}`);
      invalidate("/api/sessions");
      const route = parse();
      if (route.session === session.id) navigate(pathFor("agents"));
      else if (route.with === session.id) navigate(pathFor("agents", route.session));
    } catch (error) { toast(errorText(error)); }
  }
  async function archive() {
    try {
      await api.patch(`/api/sessions/${session.id}`, { archived: !session.archived });
      invalidate("/api/sessions");
    } catch (error) { toast(errorText(error)); }
  }
  const startRename = () => { setTitle(session.title); setEditing(true); };
  const items: MenuItem[] = [
    { label: t("session.rename"), icon: "pen", onSelect: startRename },
    { label: t("session.project.move"), icon: "folder", onSelect: () => setMoving(true) },
    ...(opts.onProject ? [{ label: t("project.settings.for", { name: opts.projectName ?? session.project }), icon: "settings" as const, onSelect: opts.onProject }] : []),
    { label: t(session.archived ? "agents.restore" : "agents.archive"), icon: "archive", onSelect: () => void archive() },
    { label: t("common.delete"), icon: "trash", danger: true, onSelect: () => void remove() },
  ];
  const layers = <>
    {moving && <MoveSessionSheet sessionId={session.id} current={session.project_id} currentOwn={!!session.workspace_own} onClose={() => setMoving(false)} onMoved={() => invalidate("/api/sessions")} toast={toast} />}
    {editing && <Sheet title={t("session.rename")} onClose={() => setEditing(false)} size="narrow">
      <form onSubmit={(event) => { event.preventDefault(); void rename(); }}>
        <input className="field" aria-label={t("session.rename")} autoFocus value={title} maxLength={128} onChange={(event) => setTitle(event.target.value)} />
        <button className="btn primary" type="submit" disabled={saving || !title.trim()}>{t("common.save")}</button>
      </form>
    </Sheet>}
  </>;
  return { items, layers, run: { rename: startRename, archive, remove } };
}

/** The form for a new agent: a name, a first task and where it works; the loop and the tools sit behind Advanced. */
export function NewAgentSheet({ onClose, onCreated, toast, project: initial = "" }: { onClose: () => void; onCreated: (id: string) => void; toast: (t: string) => void; project?: string }) {
  const [title, setTitle] = useState("");
  const [prompt, setPrompt] = useState("");
  const [toolsOff, setToolsOff] = useState<string[]>([]);
  const [loopOn, setLoopOn] = useState(false);
  const [loopText, setLoopText] = useState("");
  const [loopMode, setLoopMode] = useState<"interval" | "dynamic">("interval");
  const [loopMinutes, setLoopMinutes] = useState("10");
  const [loopMax, setLoopMax] = useState("");
  const [project, setProject] = useState(initial);
  const [ownDirectory, setOwnDirectory] = useState(false);
  const [folder, setFolder] = useState("");
  const [preset, setPreset] = useState("");
  const [advanced, setAdvanced] = useState(false);
  const [busy, setBusy] = useState(false);
  const projects = useProjects();
  const run = useRunOn(project);
  const chosen = (projects.data ?? []).find((p) => p.id === project);
  const folderChoice = chosen && offersFolderChoice(chosen) ? agentFolders(chosen) : [];
  // Empty is the primary: the session then follows whichever folder is first, as one that named
  // none always has. Only another folder is named outright.
  const chosenFolder = folderChoice.find((f) => f.id === folder && f.position !== 0);
  const settings = useQuery<Settings>("/api/settings", { staleMs: 60000 });
  const presets = settings.data?.presets ?? {};
  const defaultPreset = settings.data?.model?.preset ?? "";
  const presetLabel = (id: string) => {
    const p = presets[id];
    return p ? p.label || `${p.provider}/${p.model}` : id;
  };

  async function create() {
    if (!title.trim() || busy) return;
    setBusy(true);
    try {
      const loop = loopOn && loopText.trim()
        ? { instruction: loopText.trim(), mode: loopMode, interval_minutes: loopMode === "interval" ? Math.max(1, Number(loopMinutes) || 10) : null, max_runs: loopMax.trim() ? Math.max(1, Number(loopMax) || 1) : null }
        : undefined;
      const created = await api.post<{ id: string }>("/api/sessions", { title: title.trim(), prompt: prompt.trim() || undefined, tools_off: toolsOff, loop, project_id: project || undefined, folder_id: chosenFolder?.id, own_directory: ownDirectory, preset: preset || undefined, ...run.body });
      onClose();
      onCreated(created.id);
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Sheet title={t("newagent.title")} onClose={onClose}>
      <label className="field">{t("common.name")}</label>
      <input className="field" autoFocus value={title} onChange={(e) => setTitle(e.target.value)} placeholder={t("newagent.name.placeholder")} onKeyDown={(e) => e.key === "Enter" && create()} />
      <label className="field">{t("newagent.first")}</label>
      <textarea className="field" rows={3} value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder={t("newagent.first.placeholder")} />
      <label className="field">{t("newagent.model")}</label>
      <select className="field" value={preset} onChange={(e) => setPreset(e.target.value)}>
        <option value="">{t("newagent.model.default")}{defaultPreset ? ` · ${presetLabel(defaultPreset)}` : ""}</option>
        {Object.keys(presets).map((id) => (
          <option key={id} value={id}>{presetLabel(id)}</option>
        ))}
      </select>
      <label className="field">{t("newagent.where")}</label>
      <select className="field" value={project} onChange={(e) => { setProject(e.target.value); setFolder(""); }}>
        <option value="">{t("newagent.where.newproject")}</option>
        {(projects.data ?? []).map((p) => (
          <option key={p.id} value={p.id} disabled={!projectReachable(p)}>
            {p.name} · {projectPath(p)}{projectReachable(p) ? "" : t("newagent.where.unmounted")}
          </option>
        ))}
      </select>
      {folderChoice.length > 0 && (
        <>
          <label className="field" htmlFor="newagent-folder">{t("newagent.folder")}</label>
          <select id="newagent-folder" className="field" value={chosenFolder?.id ?? ""} onChange={(e) => setFolder(e.target.value)}>
            {folderChoice.map((f) => (
              <option key={f.id} value={f.position === 0 ? "" : f.id} disabled={!f.reachable}>
                {folderName(f)} · {f.path}{f.position === 0 ? t("newagent.folder.primary") : ""}{f.reachable ? "" : t("newagent.where.unmounted")}
              </option>
            ))}
          </select>
        </>
      )}
      {chosen ? <div className="sub">{t("newagent.where.inside", { root: chosenFolder?.path ?? projectPath(chosen) })}</div> : <div className="sub">{t("newagent.where.newproject.hint")}</div>}
      {run.offered && (
        <>
          <label className="field" htmlFor="newagent-runon">{t("runon.label")}</label>
          <select id="newagent-runon" className="field" value={run.env} onChange={(e) => run.pick(e.target.value as RunEnv)}>
            <option value="container">{t("folder.env.container.long")}</option>
            <option value="host" disabled={!run.hostReady}>{t("folder.env.host.long")}</option>
          </select>
          <div className="sub">{run.hostReady ? t(`runon.${run.env}.hint`) : t("runon.host.down")}</div>
        </>
      )}
      {chosen && <label className="toggle-row"><input type="checkbox" checked={ownDirectory} onChange={(e) => setOwnDirectory(e.target.checked)} /><span>{t("newagent.directory.own")}</span><span className="sub">{t("newagent.directory.own.hint")}</span></label>}
      <button type="button" className="disclosure" onClick={() => setAdvanced((v) => !v)} aria-expanded={advanced}>
        <span className={`chev ${advanced ? "down" : ""}`}>›</span> {t("newagent.advanced")}{loopOn ? t("newagent.advanced.loop") : ""}{toolsOff.length ? t("newagent.advanced.tools", { n: toolsOff.length }) : ""}
      </button>
      {advanced && (
        <div className="disclosure-body">
          <div className="sub">{t("newagent.shared")}</div>
          <label className="toggle-row">
            <input type="checkbox" checked={loopOn} onChange={(e) => setLoopOn(e.target.checked)} />
            <span>{t("newagent.loop")}</span>
            <span className="sub">{t("newagent.loop.hint")}</span>
          </label>
          {loopOn && (
            <div className="loop-form">
              <label className="field">{t("newagent.loop.label")}</label>
              <textarea className="field" rows={3} value={loopText} onChange={(e) => setLoopText(e.target.value)} placeholder={t("newagent.loop.placeholder")} />
              <div className="composer-row">
                <select className="field" value={loopMode} onChange={(e) => setLoopMode(e.target.value as "interval" | "dynamic")}>
                  <option value="interval">{t("newagent.loop.interval")}</option>
                  <option value="dynamic">{t("loop.selfpaced")}</option>
                </select>
                {loopMode === "interval" && <input className="field" type="number" min={1} style={{ maxWidth: 110 }} value={loopMinutes} onChange={(e) => setLoopMinutes(e.target.value)} aria-label={t("newagent.loop.minutes")} />}
                <input className="field" type="number" min={1} style={{ maxWidth: 130 }} placeholder={t("newagent.loop.max")} value={loopMax} onChange={(e) => setLoopMax(e.target.value)} aria-label={t("newagent.loop.max")} />
              </div>
              <div className="sub">{t("newagent.loop.note")}</div>
            </div>
          )}
          <ToolPicker off={toolsOff} onChange={setToolsOff} note={t("newagent.tools.note")} />
        </div>
      )}
      <div className="sheet-foot">
        <button className="btn ghost" onClick={onClose}>{t("common.cancel")}</button>
        <button className="btn primary" onClick={create} disabled={busy || !title.trim() || (loopOn && !loopText.trim())}>
          {t("common.create")}
        </button>
      </div>
    </Sheet>
  );
}

export function useSessionTitles(): Record<string, string> {
  const { data } = useQuery<SessionList>("/api/sessions", { staleMs: 15000 });
  return useMemo(() => Object.fromEntries((data?.sessions ?? []).map((s) => [s.id, s.title])), [data]);
}
