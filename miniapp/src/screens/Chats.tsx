// Every chat, on a phone: borderless 60 px rows grouped by project (a project with more than one chat
// keeps its own section) and by day for the rest, the status chips above them and the search behind
// an icon. A row's commands — the old "…" and "+" — live in its long-press sheet and behind its ⋮, and
// a swipe reaches the three most used; nothing the old row offered is gone. What the operator pinned
// is a block above everything, the same block the desktop's sidebar draws. The desktop keeps
// Sessions.tsx; both read the same listing and the same commands (useSessionCommands).

import { useEffect, useMemo, useRef, useState } from "react";
import { api, type Project, type SessionList, type SessionSummary } from "../api";
import { relTime, shortModel, untilShort } from "../format";
import { type Folder, type Row, agentName, arrange, dayGroup, kindOf, liveRow, splitPinned } from "../grouping";
import { pinCommand } from "../pins";
import { plural, t } from "../i18n";
import { Icon } from "../icons";
import { agentsListingOf } from "../mode";
import { ProjectSettingsSheet, useEnvironments, useProjects } from "../projects";
import { navigate, pathFor, useRoute } from "../router";
import { useOffline, useQuery } from "../store";
import { useStreamUp } from "../events";
import { errorText } from "../ui";
import { fmtInterval, statusWord } from "../ui/components";
import type { MenuItem } from "../ui/dialogs";
import { ActionSheet, Banner, Chip, ChipBar, EmptyState, IconButton, ListRow, NewChatPill, SectionHeader, TopBar } from "../ui/phone";
import { NewAgentSheet, useSessionCommands } from "./Sessions";

type View = "all" | "attention" | "working" | "loops" | "archive";
type SearchList = SessionList & { semantic: boolean; reason: string; partial: boolean; indexing: boolean };

const CLOSED = "daedalus.chats.closed.";
function sectionClosed(key: string): boolean {
  try { return localStorage.getItem(CLOSED + key) === "1"; } catch { return false; }
}
function rememberSection(key: string, closed: boolean): void {
  try {
    if (closed) localStorage.setItem(CLOSED + key, "1");
    else localStorage.removeItem(CLOSED + key);
  } catch { /* private mode: the section forgets between visits */ }
}

const DAYS = ["today", "yesterday", "week", "month", "older"] as const;

/**
 * The page's sections: what is pinned first, as one section (`splitPinned`, the desktop's rule);
 * then a project (`isProject`, the same rule the desktop's sidebar splits by) is a section of its
 * own, in recency order; every chat falls into its day, with its forks and subagents under it.
 * Projects with no chat in the current view are left out, as the drawer lists every project anyway.
 */
export function chatSections(all: Folder[], now = new Date(), opts: { pins?: boolean } = {}): { key: string; folder?: Folder; day?: typeof DAYS[number]; pinned?: Folder[]; rows: Row[] }[] {
  // Every pinned folder with something in the view; the page leaves out an empty project anyway.
  const { pinned, rest: folders } = splitPinned(all, { filtered: true, pins: opts.pins });
  const projects = folders.filter((f) => f.rows.length > 0 && !f.chat);
  const loose = folders.filter((f) => f.rows.length > 0 && f.chat).flatMap((f) => f.rows);
  const days = DAYS.map((day) => ({ key: `day:${day}`, day, rows: loose.filter((r) => dayGroup(r.s.last_message_at || r.s.created_at, now) === day) }))
    .filter((d) => d.rows.length > 0);
  return [...(pinned.length ? [{ key: "pinned", pinned, rows: [] }] : []), ...projects.map((f) => ({ key: f.key, folder: f, rows: f.rows })), ...days];
}

function loopLine(s: SessionSummary): string {
  const loop = s.metadata?.loop;
  if (!loop) return "";
  const cadence = loop.mode === "interval" ? t("loop.every", { t: fmtInterval(loop.interval_seconds) }) : t("loop.selfpaced");
  const runs = t("agents.loop.runs", { n: loop.run_count });
  return [cadence, runs, loop.status === "active" && loop.next_run_at ? t("ph.loop.next", { t: untilShort(loop.next_run_at) }) : statusWord(loop.status).toLowerCase()].filter(Boolean).join(" · ");
}

/** The status icon of a row: what kind of chat it is, and a dot for its state. */
function RowIcon({ s, fork }: { s: SessionSummary; fork?: boolean }) {
  const icon = s.archived ? "archive" : s.metadata?.loop ? "loop" : fork || s.metadata?.forked_from ? "fork" : "bots";
  const state = s.status === "waiting" || s.status === "running" || s.status === "failed" ? s.status : s.status === "compacting" ? "running" : "";
  return <span className="ph-sicon"><Icon name={icon} size={18} />{state && <span className={`ph-st ${state}`} />}</span>;
}

/** The meta line: the state in its colour when it asks for attention, then the model and what hangs off the chat. */
function RowMeta({ row, fork }: { row: Row; fork?: number }) {
  const s = row.s;
  // The host as a word of the meta line, as a phone row says everything else that hangs off a chat.
  const onHost = useEnvironments().data?.docker && s.env === "host";
  const live = s.status === "waiting" || s.status === "running" || s.status === "failed";
  const extra = [
    s.metadata?.loop ? loopLine(s) : "",
    fork ? t("agents.fork.at", { n: fork }) : "",
    s.model ? shortModel(s.model, 28) : "",
    row.kids.length ? plural("ph.subagents", row.kids.length) : "",
    s.workspace_own && !fork ? t("agents.own.chip") : "",
    onHost ? t("term.env.host") : "",
    s.terminals ? plural("term.count", s.terminals) : "",
    s.metadata?.subagent_of ? t("agents.orphan") : "",
  ].filter(Boolean).join(" · ");
  return (
    <>
      {live ? <span className={s.status}>{statusWord(s.status)}</span> : !s.metadata?.loop && <span>{s.status === "idle" && s.background_count ? plural("session.jobs.waiting", s.background_count) : statusWord(s.status)}</span>}
      {extra && (live || !s.metadata?.loop) && <span className="ph-sep" />}
      {extra && <span className="ph-ell">{extra}</span>}
    </>
  );
}

function ChatRow({ row, fork, projectName, toast, onOpen, onNewIn, pin }: { row: Row; fork?: number; projectName: string; toast: (text: string) => void; onOpen: (id: string) => void; onNewIn: () => void; pin?: "on" | "off" }) {
  const s = row.s;
  const [projectOpen, setProjectOpen] = useState(false);
  const projects = useProjects();
  const project = (projects.data ?? []).find((p) => p.id === s.project_id) ?? null;
  const { items, layers } = useSessionCommands(s, { onProject: project ? () => setProjectOpen(true) : undefined, projectName });
  const actions: MenuItem[] = [
    { label: t("ph.newchat.in", { name: projectName }), icon: "compose", onSelect: onNewIn },
    ...(pin ? [pinCommand(s.project_id, pin === "on")] : []),
    ...row.kids.map((kid) => ({ label: t("agents.sub.open", { name: agentName(kid) }), icon: "bots" as const, hint: statusWord(kid.status), onSelect: () => onOpen(kid.id) })),
    ...items,
  ];
  const find = (icon: string) => items.find((item) => item !== "-" && item.icon === icon) as Exclude<MenuItem, "-"> | undefined;
  const archive = find("archive"), remove = find("trash"), move = find("folder");
  const swipe = s.archived ? undefined : [
    ...(move ? [{ label: t("ph.swipe.move"), icon: "folder" as const, tone: "neutral" as const, onSelect: move.onSelect }] : []),
    ...(archive ? [{ label: archive.label, icon: "archive" as const, tone: "info" as const, onSelect: archive.onSelect }] : []),
    ...(remove ? [{ label: remove.label, icon: "trash" as const, tone: "danger" as const, onSelect: remove.onSelect }] : []),
  ];
  const meta = [projectName, statusWord(s.status), s.model ? shortModel(s.model, 32) : ""].filter(Boolean).join(" · ");
  return (
    <>
      <ListRow
        data={{ session: s.id }}
        title={agentName(s)}
        meta={<RowMeta row={row} fork={fork} />}
        lead={<RowIcon s={s} fork={!!fork} />}
        trail={<>{relTime(s.last_message_at)}{s.unread_result && <span className="ph-udot" role="img" aria-label={t("agents.unread")} title={t("agents.unread")} />}</>}
        unread={!!s.unread_result}
        onOpen={() => onOpen(s.id)}
        actions={actions}
        preview={{ title: agentName(s), meta }}
        swipe={swipe}
        swipeStart={s.archived && archive ? { label: archive.label, icon: "reload", tone: "accent", onSelect: archive.onSelect } : undefined}
      />
      {layers}
      {projectOpen && project && <ProjectSettingsSheet project={project} onClose={() => setProjectOpen(false)} onRemoved={() => setProjectOpen(false)} toast={toast} />}
    </>
  );
}

/** A row and the forks taken from it, which follow it in the same section and say where they forked. */
function lines(rows: Row[], nested = false): { row: Row; fork?: number }[] {
  return rows.flatMap((r) => [{ row: r, fork: nested ? r.s.metadata?.forked_from?.seq : undefined }, ...lines(r.forks, true)]);
}

function ProjectSection({ folder, rows, toast, onOpen, filtered }: { folder: Folder; rows: Row[]; toast: (text: string) => void; onOpen: (id: string) => void; filtered: boolean }) {
  const [closed, setClosed] = useState(() => sectionClosed(folder.key));
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState(false);
  const [menu, setMenu] = useState(false);
  const shown = !closed || filtered;
  const toggle = () => { setClosed(!closed); rememberSection(folder.key, !closed); };
  return (
    <section data-project={folder.key}>
      <SectionHeader count={folder.total} action={<>
        <IconButton icon="vdots" label={`${folder.name}: ${t("dlg.menu")}`} onClick={() => setMenu(true)} popup="menu" />
        <IconButton icon={shown ? "chevron" : "forward"} label={t(shown ? "agents.folder.hide" : "agents.folder.show", { name: folder.name })} onClick={toggle} expanded={shown} />
      </>}>{folder.name}</SectionHeader>
      {menu && <ActionSheet preview={{ title: folder.name, meta: plural("agents.count", folder.total) }} onClose={() => setMenu(false)} items={[
        { label: t("ph.newchat.in", { name: folder.name }), icon: "compose", onSelect: () => setAdding(true) },
        pinCommand(folder.key, false),
        ...(folder.system ? [{ label: t("agents.folder.tovoice"), icon: "mic" as const, onSelect: () => navigate(pathFor("voice")) }] : [{ label: t("project.settings.for", { name: folder.name }), icon: "settings" as const, onSelect: () => setEditing(true) }]),
        { label: t(shown ? "agents.folder.hide" : "agents.folder.show", { name: folder.name }), icon: shown ? "chevron" : "forward", onSelect: toggle },
      ]} />}
      {shown && <div className="ph-list">{lines(rows).map(({ row, fork }) => <ChatRow key={row.s.id} row={row} fork={fork} projectName={folder.name} toast={toast} onOpen={onOpen} onNewIn={() => setAdding(true)} />)}</div>}
      {adding && <NewAgentSheet project={folder.key} onClose={() => setAdding(false)} onCreated={onOpen} toast={toast} />}
      {editing && <ProjectSettingsSheet project={{ ...folder.project, sessions: folder.rows.map((r) => ({ id: r.s.id, title: r.s.title })) }} onClose={() => setEditing(false)} onRemoved={() => setEditing(false)} toast={toast} />}
    </section>
  );
}

/**
 * The pinned block: a pinned chat is its row, a pinned project a row that folds and unfolds its chats
 * under it. A folded pinned project still shows the chats that ask for the operator or work, as on
 * the desktop, because a pin is the last place a question should hide.
 */
function PinnedSection({ folders, toast, onOpen, onNewIn, filtered }: { folders: Folder[]; toast: (text: string) => void; onOpen: (id: string) => void; onNewIn: (project: string) => void; filtered: boolean }) {
  return (
    <section data-pinned="">
      <SectionHeader count={folders.length}><span className="ph-pinhead"><Icon name="pushpin" size={14} />{t("side.pinned")}</span></SectionHeader>
      <div className="ph-list">
        {folders.map((f) => f.chat
          ? lines(f.rows).map(({ row, fork }) => <ChatRow key={row.s.id} row={row} fork={fork} projectName={f.name} toast={toast} onOpen={onOpen} onNewIn={() => onNewIn(f.key)} pin={fork ? undefined : "on"} />)
          : <PinnedProject key={f.key} folder={f} toast={toast} onOpen={onOpen} filtered={filtered} />)}
      </div>
    </section>
  );
}

function PinnedProject({ folder, toast, onOpen, filtered }: { folder: Folder; toast: (text: string) => void; onOpen: (id: string) => void; filtered: boolean }) {
  const [closed, setClosed] = useState(() => sectionClosed(folder.key));
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState(false);
  const shown = !closed || filtered;
  const toggle = () => { setClosed(!closed); rememberSection(folder.key, !closed); };
  const all = lines(folder.rows);
  const visible = shown ? all : all.filter(({ row }) => liveRow(row));
  const actions: MenuItem[] = [
    { label: t("ph.newchat.in", { name: folder.name }), icon: "compose", onSelect: () => setAdding(true) },
    pinCommand(folder.key, true),
    ...(folder.system ? [{ label: t("agents.folder.tovoice"), icon: "mic" as const, onSelect: () => navigate(pathFor("voice")) }] : [{ label: t("project.settings.for", { name: folder.name }), icon: "settings" as const, onSelect: () => setEditing(true) }]),
    { label: t(shown ? "agents.folder.hide" : "agents.folder.show", { name: folder.name }), icon: shown ? "chevron" : "forward", onSelect: toggle },
  ];
  return (
    <div data-project={folder.key} className={`ph-pinproject ${shown ? "open" : ""}`}>
      <ListRow
        data={{ "project-head": folder.key }}
        title={folder.name}
        meta={<span className="ph-ell">{plural("agents.count", folder.total)}</span>}
        lead={<span className="ph-sicon"><Icon name={folder.system ? "mic" : "folder"} size={18} /></span>}
        trail={<span className={`ph-pinchev ${shown ? "open" : ""}`} aria-hidden><Icon name={shown ? "chevron" : "forward"} size={16} /></span>}
        label={t(shown ? "agents.folder.hide" : "agents.folder.show", { name: folder.name })}
        onOpen={toggle}
        actions={actions}
        preview={{ title: folder.name, meta: plural("agents.count", folder.total) }}
      />
      {visible.length > 0 && <div className="ph-pinkids">{visible.map(({ row, fork }) => <ChatRow key={row.s.id} row={row} fork={fork} projectName={folder.name} toast={toast} onOpen={onOpen} onNewIn={() => setAdding(true)} />)}</div>}
      {adding && <NewAgentSheet project={folder.key} onClose={() => setAdding(false)} onCreated={onOpen} toast={toast} />}
      {editing && <ProjectSettingsSheet project={{ ...folder.project, sessions: folder.rows.map((r) => ({ id: r.s.id, title: r.s.title })) }} onClose={() => setEditing(false)} onRemoved={() => setEditing(false)} toast={toast} />}
    </div>
  );
}

function Skeleton() {
  const widths = [[62, 38], [48, 44], [70, 30], [55, 35], [66, 42], [40, 33]];
  return (
    <div aria-busy="true" aria-label={t("common.loading")}>
      {widths.map(([a, b], i) => (
        <div key={i} className="ph-skrow">
          <span className="ph-sk" style={{ width: 36, height: 36, borderRadius: "50%" }} />
          <span className="grow"><span className="ph-sk" style={{ height: 14, width: `${a}%` }} /><span className="ph-sk" style={{ height: 11, width: `${b}%`, marginTop: 8 }} /></span>
        </div>
      ))}
    </div>
  );
}

export function ChatsScreen({ onOpen, toast, project: kept = "", projects = [], onPickProject }: { onOpen: (id: string) => void; toast: (t: string) => void; project?: string; projects?: Project[]; onPickProject?: (id: string) => void }) {
  const route = useRoute();
  // A project tapped in the drawer narrows this visit only, through the address: it used to set the
  // remembered lens, which then narrowed every list on the device until someone found the chip.
  const visit = route.query.get("project") ?? "";
  const project = visit || kept;
  const clearLens = () => {
    if (visit) navigate(pathFor("agents", null, { view: "chats" }), { replace: true });
    if (kept) onPickProject?.("");
  };
  const [view, setView] = useState<View>("all");
  const [searching, setSearching] = useState(route.query.get("search") === "1");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchList | null>(null);
  const [pending, setPending] = useState(false);
  const [searchError, setSearchError] = useState("");
  const [adding, setAdding] = useState<string | null>(null);
  const [extra, setExtra] = useState<SessionSummary[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [paging, setPaging] = useState(false);
  const field = useRef<HTMLInputElement>(null);
  const offline = useOffline();
  const live = useStreamUp();
  const serverView = view === "loops" ? "all" : view;
  const { data, error, loading, refresh } = useQuery<SessionList>(`/api/sessions?view=${serverView}`, { pollMs: live ? 60000 : 5000, staleMs: 3000 });
  const all = useQuery<SessionList>("/api/sessions?view=all", { pollMs: live ? 60000 : 5000, staleMs: 3000 });
  const lens = projects.find((p) => p.id === project);
  const typed = query.trim();

  useEffect(() => { setExtra([]); setNext(data?.next_cursor ?? null); }, [data, view]);
  useEffect(() => { if (searching) field.current?.focus(); }, [searching]);
  useEffect(() => {
    setResults(null);
    setSearchError("");
    setPending(!!typed);
    if (!typed) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void fetch(`/api/sessions/search?q=${encodeURIComponent(typed)}&project=${encodeURIComponent(project)}`, { headers: api.authHeaders(), signal: controller.signal })
        .then(async (response) => {
          if (!response.ok) throw new Error(t("search.failed"));
          const answer: SearchList = await response.json();
          if (!controller.signal.aborted) setResults(answer);
        })
        .catch(() => { if (!controller.signal.aborted) setSearchError(t("search.failed")); })
        .finally(() => { if (!controller.signal.aborted) setPending(false); });
    }, 250);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [typed, project]);

  const listing = useMemo(() => (data && Array.isArray(data.sessions) ? agentsListingOf({ ...data, sessions: [...data.sessions, ...extra] }) : null), [data, extra]);
  const arranged = useMemo(() => (listing ? arrange(listing.sessions, listing.projects, { project, filter: view === "loops" ? "loops" : "all" }) : null), [listing, project, view]);
  // The archive lists what was put away in its own order, with nothing lifted out of it.
  const sections = useMemo(() => (arranged ? chatSections(arranged.folders, new Date(), { pins: view !== "archive" }) : []), [arranged, view]);
  // The chips count over the whole list, whichever chip is on, so a number never changes under the
  // finger that is about to press it.
  const counts = useMemo(() => {
    const top = (agentsListingOf(all.data)?.sessions ?? []).filter((s) => !s.metadata?.subagent_of && (!project || s.project_id === project));
    return {
      all: top.length,
      attention: top.filter((s) => s.needs_attention || s.status === "waiting").length,
      working: top.filter((s) => kindOf(s, false) === "working").length,
      loops: top.filter((s) => kindOf(s, false) === "loop").length,
    };
  }, [all.data, project]);
  const found = useMemo(() => agentsListingOf(results)?.sessions ?? [], [results]);
  const newChat = () => navigate(pathFor("agents"));
  const projectName = (id: string) => listing?.projects.find((p) => p.id === id)?.name ?? "";

  const top = searching ? (
    <header className="ph-top">
      <IconButton icon="back" label={t("shell.back")} onClick={() => { setSearching(false); setQuery(""); }} />
      <label className="ph-search">
        <Icon name="search" size={18} />
        <input ref={field} type="search" value={query} maxLength={500} placeholder={t("search.placeholder")} aria-label={t("search.label")} onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => { if (e.key === "Escape") { setSearching(false); setQuery(""); } }} />
      </label>
      {query && <IconButton icon="close" label={t("ph.search.clear")} onClick={() => { setQuery(""); field.current?.focus(); }} />}
    </header>
  ) : (
    <TopBar center title={t("ph.chats")} pip={counts.attention > 0} actions={<IconButton icon="search" label={t("search.label")} onClick={() => setSearching(true)} />} />
  );

  let body: React.ReactNode;
  if (searching && typed) {
    body = (
      <>
        <div className="ph-page-pad">
          <Banner tone={searchError ? "bad" : "info"} icon={searchError ? "alert" : "search"}
            sub={<>{results?.indexing && t("search.indexing")} {results?.partial && t("search.partial")} {!pending && !searchError && results && !results.semantic && <a href={pathFor("settings", "components")}>{t("search.enable")}</a>}</>}>
            {pending ? t("search.loading") : searchError || (results?.semantic ? t("search.semantic") : t("search.exact"))}
          </Banner>
        </div>
        {results && !pending && <SectionHeader>{plural("ph.chats.count", found.length)}</SectionHeader>}
        {results && !pending && found.length === 0 && <EmptyState icon="search" title={t("common.nothing")} />}
        <div className="ph-list">
          {found.map((s) => (
            <ListRow key={s.id} data={{ session: s.id }} title={agentName(s)} lead={<RowIcon s={s} />} trail={relTime(s.last_message_at)} onOpen={() => onOpen(s.id)}
              meta={s.match ? <span className="ph-snip">{s.match.snippet}</span> : <span className="ph-ell">{projectName(s.project_id) || s.project}</span>} />
          ))}
        </div>
      </>
    );
  } else if (searching) {
    body = <EmptyState icon="search" title={t("search.label")} body={t("search.placeholder")} />;
  } else if (error && !data) {
    body = <EmptyState icon="alert" tone="bad" title={t("agents.error")} body={error} action={<button type="button" className="ph-btn primary" onClick={() => refresh()}>{t("common.retry")}</button>} />;
  } else if (loading && !data) {
    body = <Skeleton />;
  } else if (arranged && arranged.total === 0 && view === "all") {
    body = <EmptyState icon="bots" title={lens ? t("agents.empty.project", { name: lens.name }) : t("agents.empty")} body={t("agents.empty.sub")}
      action={<button type="button" className="ph-btn accent" onClick={newChat}><Icon name="compose" size={18} />{t("ph.newchat")}</button>} />;
  } else if (arranged && arranged.shown === 0) {
    body = <EmptyState icon="check" tone="ok" title={t("common.nothing")} body={t(`ph.empty.${view}`)} action={<button type="button" className="ph-btn" onClick={() => setView("all")}>{t("ph.showall")}</button>} />;
  } else {
    body = (
      <>
        {view === "archive" && arranged && <SectionHeader count={arranged.shown}>{t("ph.archived")}</SectionHeader>}
        {sections.map((section) => section.pinned
          ? <PinnedSection key={section.key} folders={section.pinned} toast={toast} onOpen={onOpen} onNewIn={setAdding} filtered={view !== "all"} />
          : section.folder && view !== "archive"
          ? <ProjectSection key={section.key} folder={section.folder} rows={section.rows} toast={toast} onOpen={onOpen} filtered={view !== "all"} />
          : (
            <section key={section.key}>
              {view !== "archive" && <SectionHeader count={section.day === "week" || section.day === "month" || section.day === "older" ? section.rows.length : undefined}>{section.folder ? section.folder.name : t(`ph.day.${section.day}`)}</SectionHeader>}
              <div className="ph-list">
                {lines(section.rows).map(({ row, fork }) => <ChatRow key={row.s.id} row={row} fork={fork} projectName={section.folder?.name ?? projectName(row.s.project_id)} toast={toast} onOpen={onOpen} onNewIn={() => setAdding(row.s.project_id)} pin={!section.folder && !fork && view !== "archive" ? "off" : undefined} />)}
              </div>
            </section>
          ))}
        {next && (
          <button type="button" className="ph-btn ph-more-btn" disabled={paging} onClick={async () => {
            setPaging(true);
            try {
              const page = await api.get<SessionList>(`/api/sessions?view=${serverView}&cursor=${encodeURIComponent(next)}`);
              setExtra((held) => [...held, ...page.sessions.filter((row) => !data?.sessions.some((first) => first.id === row.id) && !held.some((old) => old.id === row.id))]);
              setNext(page.next_cursor ?? null);
            } catch (e) { toast(errorText(e)); }
            finally { setPaging(false); }
          }}>{t(paging ? "agents.loading" : "agents.more")}</button>
        )}
      </>
    );
  }

  return (
    <div className="ph-page ph-chats">
      {top}
      {offline && <Banner strip tone="bad" icon="offline">{t("app.offline")}</Banner>}
      {!searching && (
        <ChipBar label={t("agents.filter.label")}>
          {lens && (visit || onPickProject) && <Chip on onClick={clearLens} label={t("ph.lens.clear", { name: lens.name })}><span className="ph-lens"><Icon name="folder" size={16} />{lens.name}<Icon name="close" size={14} /></span></Chip>}
          <Chip on={view === "all"} count={all.data ? counts.all : undefined} onClick={() => setView("all")}>{t("agents.filter.all")}</Chip>
          <Chip on={view === "attention"} tone="warn" count={all.data ? counts.attention : undefined} onClick={() => setView("attention")}>{t("ph.filter.attention")}</Chip>
          <Chip on={view === "working"} count={all.data ? counts.working : undefined} onClick={() => setView("working")}>{t("agents.filter.working")}</Chip>
          <Chip on={view === "loops"} count={all.data ? counts.loops : undefined} onClick={() => setView("loops")}>{t("ph.filter.loops")}</Chip>
          <Chip on={view === "archive"} onClick={() => setView("archive")}>{t("agents.filter.archive")}</Chip>
        </ChipBar>
      )}
      <div className="ph-page-body list">{body}</div>
      {!searching && <NewChatPill floating label={t("ph.newchat")} onClick={newChat} />}
      {adding !== null && <NewAgentSheet project={adding} onClose={() => setAdding(null)} onCreated={onOpen} toast={toast} />}
    </div>
  );
}
