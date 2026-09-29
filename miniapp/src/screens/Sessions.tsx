import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api, Project, ProjectFolder, SessionList, SessionSummary, Settings } from "../api";
import { agentFolders, folderName, offersFolderChoice, projectPath, projectReachable } from "../folders";
import { Avatar, Dot, Skeleton, Status, ToolPicker, fmtInterval, statusWord } from "../components";
import { OverflowMenu, Sheet, confirmDialog, toast } from "../dialogs";
import { relTime, shortModel, untilShort } from "../format";
import { Folder, Row as RowModel, agentName, arrange, folderOpen, rememberFolder } from "../grouping";
import { Icon } from "../icons";
import { MoveSessionSheet, ProjectChip, ProjectSettingsSheet, useProjects } from "../projects";
import { navigate, parse, pathFor } from "../router";
import { agentsListing } from "../mode";
import { PageHeader, go, screenTitle } from "../shell";
import { invalidate, useQuery } from "../store";
import { useStreamUp } from "../events";
import { WindowedRows } from "../virtual";
import { errorText } from "../ui";
import { plural, t } from "../i18n";

type SearchList = SessionList & { semantic: boolean; reason: string; partial: boolean; indexing: boolean };

export function SessionsScreen({ onOpen, toast, current, compact, bare, project = "", projects = [], onProjects }: { onOpen: (id: string) => void; toast: (t: string) => void; current?: string; compact?: boolean; bare?: boolean; project?: string; projects?: Project[]; onProjects?: () => void }) {
  const [view, setView] = useState<"all" | "attention" | "working" | "archive">("all");
  const listUrl = `/api/sessions?view=${view}`;
  // A run starting, finishing or asking arrives as an event while the stream is up, and the list is
  // read again then; the poll every minute is only the net under it.
  const live = useStreamUp();
  const { data, error, loading } = useQuery<SessionList>(listUrl, { pollMs: live ? 60000 : 5000, staleMs: 3000 });
  const [extra, setExtra] = useState<SessionSummary[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [paging, setPaging] = useState(false);
  const [creating, setCreating] = useState(false);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchList | null>(null);
  const [pending, setPending] = useState(false);
  const [searchError, setSearchError] = useState("");
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
  const searchField = <input type="search" className="field search" placeholder={t("search.placeholder")} value={query} maxLength={500} onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => { if (e.key === "Escape") setQuery(""); }} aria-label={t("search.label")} />;
  const inProject = projects.find((p) => p.id === project);

  // The Agents list never shows what belongs to orchestration mode, found by a search or not.
  const raw = searching ? results : data ? { ...data, sessions: [...data.sessions, ...extra], next_cursor: next } : data;
  const listing = useMemo(() => (raw ? agentsListing(raw) : raw), [raw]);
  const sessions = listing?.sessions ?? [];
  const folders = useMemo(
    () => arrange(sessions, listing?.projects ?? [], { project, results: searching }),
    [listing, project, searching],
  );

  // In the sidebar the screen is the column: no page header, no filter chips, one row of controls
  // with the project switcher in it, and the search field under that row when it is open.
  // Under the start canvas the phone has no column, so the same list is the page: search and the
  // filters, without a second title and a second way to create a chat.
  const head = bare ? (
    searchField
  ) : compact ? (
    <>
      <div className="sidebar-head">
        <button className="sidebar-new-agent" onClick={() => setCreating(true)}>
          <Icon name="plus" size={17} />
          <span>{t("agents.new")}</span>
        </button>
        {onProjects && <ProjectChip projects={projects} current={project} onOpen={onProjects} />}
      </div>
      {searchField}
    </>
  ) : (
      <PageHeader
        title={inProject ? inProject.name : screenTitle("agents")}
        subtitle={data ? `${plural("agents.count", folders.total)}${folders.active ? ` · ${t("agents.active", { n: folders.active })}` : ""}${inProject ? ` · ${projectPath(inProject)}` : ""}` : undefined}
        actions={
          <>
            {onProjects && <button className="iconbtn" onClick={onProjects} title={t("shell.projects")} aria-label={t("shell.projects")}><Icon name="skill" /></button>}
            <button className="iconbtn primary" onClick={() => setCreating(true)} title={t("agents.new")} aria-label={t("agents.new")}><Icon name="plus" /></button>
          </>
        }
      >
        {searchField}
      </PageHeader>
  );
  return (
    <>
      {head}
      <div className="screen agents-screen">
        {!compact && !searching && <div className="chips agent-filters" role="group" aria-label={t("agents.filter.label")}>
          {(["all", "attention", "working", "archive"] as const).map((name) => (
            <button key={name} className={`chip ${view === name ? "selected" : ""}`} aria-pressed={view === name} onClick={() => setView(name)}>{t(`agents.filter.${name}`)}</button>
          ))}
        </div>}
        {searching && <div className="search-notice sub" role="status">
          {pending ? t("search.loading") : searchError || (results?.semantic ? t("search.semantic") : t("search.exact"))}
          {!pending && !searchError && results && !results.semantic && <> <a href="/app/settings/components">{t("search.enable")}</a></>}
          {results?.indexing && <span> {t("search.indexing")}</span>}
          {results?.partial && <span> {t("search.partial")}</span>}
        </div>}
        {loading && !error && <Skeleton rows={5} />}
        {error && !data && <div className="empty"><b>{t("agents.error")}</b><div>{error}</div></div>}
        {data && !searching && folders.total === 0 && (
          <div className="empty">
            <b>{inProject ? t("agents.empty.project", { name: inProject.name }) : t("agents.empty")}</b>
            <div>{inProject ? t("agents.empty.project.sub", { root: projectPath(inProject) }) : t("agents.empty.sub")}</div>
            <button className="btn primary" onClick={() => setCreating(true)}>{t("agents.empty.create")}</button>
          </div>
        )}
        {((results && !pending && folders.shown === 0) || (!searching && data && folders.total > 0 && folders.shown === 0)) && <div className="empty">{t("common.nothing")}</div>}
        {folders.folders.map((f) => (
          <FolderSection key={f.key} folder={f} onOpen={onOpen} current={current} filtered={searching || view !== "all"} compact={compact} toast={toast} />
        ))}
        {!searching && next && <button className="btn ghost load-more" disabled={paging} onClick={async () => {
          setPaging(true);
          try {
            const page = await api.get<SessionList>(`/api/sessions?view=${view}&cursor=${encodeURIComponent(next)}`);
            setExtra((held) => [...held, ...page.sessions.filter((row) => !data?.sessions.some((first) => first.id === row.id) && !held.some((old) => old.id === row.id))]);
            setNext(page.next_cursor ?? null);
          } catch (e) { toast(errorText(e)); }
          finally { setPaging(false); }
        }}>{t(paging ? "agents.loading" : "agents.more")}</button>}
      </div>
      {creating && <NewAgentSheet onClose={() => setCreating(false)} onCreated={onOpen} toast={toast} project={project} />}
    </>
  );
}

/**
 * One folder: a project, or the bucket of agents that work in a directory of their own.
 *
 * Collapsed to its header until it is opened, and which folders are open is remembered per folder.
 * A closed folder is not in the DOM at all, and an open one is windowed: the rows near the viewport
 * are rendered and the rest are two spacers, so an installation with hundreds of agents costs the
 * same whether its folders are open or shut. The sidebar carries this list on every screen, which is
 * what makes the difference worth having.
 *
 * A search or a filter opens what it found: leaving a match behind a closed folder would be a screen
 * that answers "nothing" while holding the answer.
 */
type FolderProps = { toast: (text: string) => void; folder: Folder; onOpen: (id: string) => void; current?: string; filtered: boolean; compact?: boolean };

/** One row per line the folder draws: an agent, then the forks taken from it, then their forks.
 *  The nesting is in the model and not in the DOM, so the rows can be windowed as one list. */
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

/** Whether an open folder would look the same. Without it an installation with hundreds of agents
 *  reconciles all of them on every poll for a list that did not change a character; `sig` is built
 *  once while the folders are arranged, so this costs a string comparison. */
function sameFolder(a: FolderProps, b: FolderProps): boolean {
  const l = a.folder, r = b.folder;
  if (l.single !== r.single || l.system !== r.system || a.toast !== b.toast) return false;
  if (a.filtered !== b.filtered || a.current !== b.current || a.onOpen !== b.onOpen || a.compact !== b.compact) return false;
  if (l.key !== r.key || l.name !== r.name || l.total !== r.total || l.active !== r.active || l.loops !== r.loops || l.last_message_at !== r.last_message_at) return false;
  if ((l.project ? projectPath(l.project) : undefined) !== (r.project ? projectPath(r.project) : undefined)) return false;
  return l.sig === r.sig;
}

export const FolderSection = memo(function FolderSection({ folder, onOpen, current, filtered, compact, toast }: FolderProps) {
  // The narrow sidebar always gives a project its own header: embedding its only session there
  // made the session title compete with two independent sets of actions and disappear entirely.
  const single = folder.single && !folder.system && !compact;
  const [open, setOpen] = useState(() => folderOpen(folder.key));
  const [editing, setEditing] = useState(false);
  const [adding, setAdding] = useState(false);
  const editProject = useCallback(() => setEditing(true), []);
  const wasSingle = useRef(single);
  useEffect(() => {
    if (wasSingle.current && !single) setOpen(true);
    wasSingle.current = single;
  }, [single]);
  const section = useRef<HTMLElement | null>(null);
  const becomingFolder = wasSingle.current && !single;
  const focusedAgent = becomingFolder ? section.current?.querySelector<HTMLElement>(".erow:focus")?.dataset.session : undefined;
  useLayoutEffect(() => {
    if (focusedAgent) [...(section.current?.querySelectorAll<HTMLElement>(".erow") ?? [])].find((row) => row.dataset.session === focusedAgent)?.focus();
  }, [focusedAgent]);
  const rows = useMemo(() => lines(folder.rows), [folder.rows]);
  const keys = useMemo(() => rows.map((line) => line.row.s.id), [rows]);
  // The folder holding the open session opens itself, without remembering it: a sidebar whose
  // current row is behind a closed folder answers "where am I" with nothing.
  const mine = holds(folder.rows, current);
  const showing = open || becomingFolder || (!single && mine) || (filtered && folder.rows.length > 0);
  const toggle = () => {
    const next = !open;
    setOpen(next);
    rememberFolder(folder.key, next);
  };
  // The Voice folder is the home of the voice agents, but the word in the list is the mode: the row
  // goes to the voice screen and the chevron beside it — its own button, with its own label — is
  // what opens the list of agents underneath, even when there is only one. Ordinary projects
  // with one agent show its conversation row; larger projects use a single disclosure header.
  const inside = (
    <>
      <Icon name={folder.system ? "mic" : "folder"} size={16} />
      {/* Two boxes that only a phone draws: the name on one line, the counts and the time under it.
          Everywhere else they are `display: contents` and the row is the single line it always was.
          In one wrapping line the name was what gave way — "Voice" read "V.." beside a gap — and
          the time fell onto a line of its own wherever the break happened to land. */}
      <span className="folder-text">
        <span className="folder-title">
          <span className="folder-name truncate">{folder.name}</span>
          {folder.active > 0 && <span className="folder-live" title={t("agents.active", { n: folder.active })}><Dot status="running" /></span>}
        </span>
        <span className="folder-meta">
          {compact ? (
            <span className="folder-counts sub num" title={plural("agents.count", folder.total)}>{folder.total}</span>
          ) : (
            <span className="folder-counts sub">
              {plural("agents.count", folder.total)}
              {folder.active > 0 && ` · ${t("agents.active", { n: folder.active })}`}
              {folder.loops > 0 && ` · ${t("agents.filter.loops", { n: folder.loops })}`}
            </span>
          )}
          {!compact && folder.last_message_at && <span className="folder-time sub num" title={new Date(folder.last_message_at).toLocaleString()}>{relTime(folder.last_message_at)}</span>}
        </span>
      </span>
    </>
  );
  const chevron = <span className={`chev ${showing ? "down" : ""}`} aria-hidden>›</span>;
  return (
    <section ref={section} data-project={folder.key} className={`folder ${single ? "single" : ""} ${folder.system ? "system" : ""} ${showing ? "open" : ""}`}>
      <div className="folder-top">
        {folder.system ? (
          <div className="folder-head linked">
            <button className="folder-disclose" onClick={toggle} aria-expanded={showing} aria-label={t(showing ? "agents.folder.hide" : "agents.folder.show", { name: folder.name })} title={t(showing ? "agents.folder.hide" : "agents.folder.show", { name: folder.name })}>
              {chevron}
            </button>
            <a className="folder-go" href={pathFor("voice")} onClick={(e) => go(e, pathFor("voice"))} title={t("agents.folder.tovoice")}>
              {inside}
            </a>
          </div>
        ) : single ? <>
          <button className="iconbtn small quiet folder-expand" onClick={toggle} aria-expanded={showing} aria-label={t("search.expand", { name: folder.name })}>{chevron}</button>
          <Row s={folder.rows[0].s} kids={[]} onOpen={onOpen} current={current === folder.rows[0].s.id} compact={compact} projectName={folder.name} onProject={editProject} />
        </> : (
          <button className="folder-head" onClick={toggle} aria-expanded={showing}>
            {chevron}
            {inside}
          </button>
        )}
        <button className="btn small folder-add" onClick={() => setAdding(true)} title={t("agents.new")} aria-label={t("agents.new")}><Icon name="plus" size={16} /></button>
        {!single && <button className="iconbtn small quiet folder-actions" onClick={editProject} aria-label={t("project.settings.for", { name: folder.name })}><Icon name="more" size={16} /></button>}
      </div>
      {showing && !compact && <div className="folder-root sub mono truncate" title={projectPath(folder.project)}>{projectPath(folder.project)}</div>}
      {showing && folder.rows.length === 0 && <div className="folder-empty sub">{filtered ? t("common.nothing") : t("agents.folder.none")}</div>}
      {showing && !single && (
        <WindowedRows
          keys={keys}
          host={section}
          rowSelector=":scope > .erow"
          estimate={compact ? 40 : 72}
          render={(i) => (
            <Row
              s={rows[i].row.s}
              kids={rows[i].row.kids}
              onOpen={onOpen}
              current={current === rows[i].row.s.id || rows[i].row.kids.some((c) => c.id === current)}
              fork={rows[i].fork}
              compact={compact}
            />
          )}
        />
      )}
      {editing && <ProjectSettingsSheet project={{ ...folder.project, sessions: folder.rows.map((r) => ({ id: r.s.id, title: r.s.title })) }} onClose={() => setEditing(false)} onRemoved={() => setEditing(false)} toast={toast} />}
      {adding && <NewAgentSheet project={folder.key} onClose={() => setAdding(false)} onCreated={onOpen} toast={toast} />}
    </section>
  );
}, sameFolder);

/** "Loop every 40m · run #4 · next in 12m", or the reason it is paused. */
function loopLine(s: SessionSummary): string {
  const loop = s.metadata?.loop;
  if (!loop) return "";
  const cadence = loop.mode === "interval" ? t("loop.every", { t: fmtInterval(loop.interval_seconds) }) : t("loop.selfpaced");
  const runs = `${t("agents.loop.runs", { n: loop.run_count })}${loop.max_runs ? `/${loop.max_runs}` : ""}`;
  if (loop.status === "active") return `${t("agents.loop.line", { cadence, runs })}${loop.next_run_at ? t("agents.loop.next", { t: untilShort(loop.next_run_at) }) : ""}`;
  const why = loop.pause_note || loop.stop_reason || "";
  return `${t("agents.loop.stopped", { status: statusWord(loop.status).toLowerCase() })}${why ? `: ${why.slice(0, 80)}` : ""} · ${runs}`;
}

/** What a row actually draws, so a fresh read of the list does not reconcile a folder that did not change.
 *
 *  The list is re-fetched whole and every object in it is new each time, so identity says nothing;
 *  this says what the row would look different for. The children are in it because a subagent's
 *  status is drawn under its leader. The unread result is in it for the same reason: without it the
 *  memo keeps the old row and the dot never appears. */
function sameRow(a: RowProps, b: RowProps): boolean {
  const l = a.s, r = b.s;
  if (l.id !== r.id || l.title !== r.title || l.status !== r.status || !!l.unread_result !== !!r.unread_result || l.last_message_at !== r.last_message_at || l.model !== r.model || l.workspace_path !== r.workspace_path) return false;
  if ((l.terminals ?? 0) !== (r.terminals ?? 0)) return false;
  if (a.projectName !== b.projectName || l.match?.snippet !== r.match?.snippet) return false;
  if (a.onProject !== b.onProject) return false;
  if (a.current !== b.current || a.compact !== b.compact || a.fork?.seq !== b.fork?.seq || a.fork?.of !== b.fork?.of) return false;
  if (JSON.stringify(l.metadata?.loop ?? null) !== JSON.stringify(r.metadata?.loop ?? null)) return false;
  if (a.kids.length !== b.kids.length) return false;
  return a.kids.every((k, i) => k.id === b.kids[i].id && k.status === b.kids[i].status && k.title === b.kids[i].title);
}

/** The session's running terminals, as a pill beside the time; nothing when it has none. */
function TerminalCount({ n }: { n?: number }) {
  if (!n) return null;
  const label = plural("term.count", n);
  return (
    <span className="erow-terms num" title={label} aria-label={label}>
      <Icon name="terminal" size={11} />
      {n}
    </span>
  );
}

type RowProps = { projectName?: string; onProject?: () => void; s: SessionSummary; kids: SessionSummary[]; onOpen: (id: string) => void; current?: boolean; fork?: { of: string; seq: number }; compact?: boolean };

const Row = memo(function Row({ s, kids, onOpen, current, fork, compact, projectName, onProject }: RowProps) {
  const [showKids, setShowKids] = useState(false);
  const orphan = !!s.metadata?.subagent_of;
  const status = s.status as Status;
  const spoken = !!projectName || status === "running" || status === "waiting" || status === "failed" || status === "compacting";
  const loop = loopLine(s);
  const visibleKids = showKids ? kids : kids.slice(0, 3);
  const open = () => onOpen(s.id);
  // A result nobody has seen yet, on any window or in Telegram: the host sets it and clears it.
  const unread = !!s.unread_result;
  const unreadDot = unread ? <span className="unread-dot" role="img" aria-label={t("agents.unread")} title={t("agents.unread")} /> : null;
  if (compact) {
    // The sidebar's row: a dot for the state, the name, the time, and a second line only when it
    // carries something the reader needs now — a loop's next run, what a fork was taken from, a
    // leader that is gone. Never the model: it is in the open session, and in the tooltip here.
    // "Needs you" sits on that second line, not beside the name: beside it, it squeezed a short
    // title like "Weekly digest" down to "Weekly di…" in the sidebar's width. The text goes in a
    // span because a bare text node in the flex line was clipped mid-word with no ellipsis.
    const needs = status === "waiting" || status === "failed";
    const second = projectName ? `${shortModel(s.model ?? "", 28)} · ${statusWord(status)}` : fork ? t("agents.fork.at", { n: fork.seq }) : loop || (orphan ? t("agents.orphan") : "");
    return (
      <div data-session={s.id} className={`erow ${status} ${current ? "current" : ""} ${fork ? "fork" : ""} ${unread ? "unread" : ""}`} title={s.model ? `${s.model} · ${s.id}` : s.id} role="link" aria-current={current ? "page" : undefined} tabIndex={0} onClick={open} onKeyDown={(e) => { if (e.target !== e.currentTarget) return; if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } }}>
        <Dot status={status} className="erow-dot" />
        <div className="erow-main">
          <div className="erow-head">
            <span className="erow-title truncate">{projectName && projectName !== agentName(s) && <span className="erow-project">{projectName} · </span>}{agentName(s)}</span>
            <TerminalCount n={s.terminals} />
            {unreadDot}
            <span className="erow-time num" title={new Date(s.last_message_at).toLocaleString()}>{relTime(s.last_message_at)}</span>
          </div>
          {s.match && <div className="search-passage truncate">{s.match.snippet}</div>}
          {(needs || second) && (
            <div className={`erow-meta ${s.metadata?.loop?.status === "paused" ? "waiting" : ""}`}>
              {needs && <span className={`erow-state ${status}`}>{statusWord(status)}</span>}
              {needs && second && <span className="sep">·</span>}
              {fork && <Icon name="fork" size={11} />}
              {second && <span>{second}</span>}
            </div>
          )}
        </div>
        <SessionRowMenu session={s} onProject={onProject} projectName={projectName} />
      </div>
    );
  }
  return (
    <div data-session={s.id} className={`erow ${status} ${current ? "current" : ""} ${fork ? "fork" : ""} ${unread ? "unread" : ""}`} title={s.workspace_path} role="link" aria-current={current ? "page" : undefined} tabIndex={0} onClick={open} onKeyDown={(e) => { if (e.target !== e.currentTarget) return; if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } }}>
      <Avatar status={status} seed={s.id} />
      <div className="erow-main">
        <div className="erow-head">
          <span className="erow-title clamp-2">{projectName && projectName !== agentName(s) && <span className="erow-project">{projectName} · </span>}{agentName(s)}</span>
          {s.workspace_own && !fork && <span className="chip tiny" title={s.workspace_path}>{t("agents.own.chip")}</span>}
          <TerminalCount n={s.terminals} />
          {unreadDot}
          <span className="erow-time num" title={new Date(s.last_message_at).toLocaleString()}>{relTime(s.last_message_at)}</span>
        </div>
        <div className={`erow-meta ${spoken ? status : ""}`}>
          {spoken && <Dot status={status} />}
          {spoken && <span className="word">{statusWord(status)}</span>}
          {spoken && s.model && <span className="sep">·</span>}
          {s.model && <span className="erow-model" title={s.model}>{shortModel(s.model, 28)}</span>}
          {orphan && <span className="sep">·</span>}
          {orphan && <span>{t("agents.orphan")}</span>}
        </div>
        {s.match && <div className="search-passage truncate">{s.match.snippet}</div>}
        {fork && <div className="erow-meta"><Icon name="fork" size={12} /> <span title={t("agents.fork.of", { name: fork.of, n: fork.seq })}>{t("agents.fork.at", { n: fork.seq })}</span></div>}
        {loop && <div className={`erow-meta ${s.metadata?.loop?.status === "paused" ? "waiting" : ""}`}><span>{loop}</span></div>}
        {kids.length > 0 && (
          <div className="erow-children" onClick={(e) => e.stopPropagation()}>
            {visibleKids.map((c) => (
              <button key={c.id} className="subrow" onClick={() => onOpen(c.id)} title={t("agents.sub.open", { name: c.metadata?.subagent_name ?? c.title })}>
                <Dot status={c.status === "idle" ? "done" : c.status} />
                <span className="truncate">{agentName(c)}</span>
                <span className="sub">{statusWord(c.status === "running" ? "running" : c.status === "waiting" ? "waiting" : c.status === "failed" ? "failed" : "done").toLowerCase()}</span>
              </button>
            ))}
            {kids.length > 3 && (
              <button className="subrow more" onClick={() => setShowKids((v) => !v)}>
                {showKids ? t("agents.sub.fewer") : plural("agents.sub.more", kids.length - 3)}
              </button>
            )}
          </div>
        )}
      </div>
      <SessionRowMenu session={s} onProject={onProject} projectName={projectName} />
    </div>
  );
}, sameRow);

function SessionRowMenu({ session, onProject, projectName }: { session: SessionSummary; onProject?: () => void; projectName?: string }) {
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
  return <span className="session-row-menu" onClick={(event) => event.stopPropagation()} onKeyDown={(event) => event.stopPropagation()}>
    <OverflowMenu small className="quiet" label={`${session.title}: ${t("dlg.menu")}`} items={[
      { label: t("session.rename"), icon: "pen", onSelect: () => { setTitle(session.title); setEditing(true); } },
      { label: t("session.project.move"), icon: "folder", onSelect: () => setMoving(true) },
      ...(onProject ? [{ label: t("project.settings.for", { name: projectName ?? session.project }), onSelect: onProject }] : []),
      { label: t(session.archived ? "agents.restore" : "agents.archive"), icon: "archive", onSelect: () => void archive() },
      { label: t("common.delete"), icon: "trash", danger: true, onSelect: () => void remove() },
    ]} />
    {moving && <MoveSessionSheet sessionId={session.id} current={session.project_id} currentOwn={!!session.workspace_own} onClose={() => setMoving(false)} onMoved={() => invalidate("/api/sessions")} toast={toast} />}
    {editing && <Sheet title={t("session.rename")} onClose={() => setEditing(false)} size="narrow">
      <form onSubmit={(event) => { event.preventDefault(); void rename(); }}>
        <input className="field" aria-label={t("session.rename")} autoFocus value={title} maxLength={128} onChange={(event) => setTitle(event.target.value)} />
        <button className="btn primary" type="submit" disabled={saving || !title.trim()}>{t("common.save")}</button>
      </form>
    </Sheet>}
  </span>;
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
      const created = await api.post<{ id: string }>("/api/sessions", { title: title.trim(), prompt: prompt.trim() || undefined, tools_off: toolsOff, loop, project_id: project || undefined, folder_id: chosenFolder?.id, own_directory: ownDirectory, preset: preset || undefined });
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
