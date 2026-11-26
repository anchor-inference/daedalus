// The list in Agents mode's sidebar, under the frame's pinned block (sidebar.tsx): the projects and the
// chats apart, newest first. Which project is a project and which is a chat's own scratch folder is
// decided once, in grouping.ts (`isProject`), by the same rule the phone's Chats page splits by.
//
// Every row is one line of 32 px in three parts, and the title has the whole middle of it:
// - a 16 px slot at the left, the same on every chat row so every title starts at one x. It holds the
//   chat's one most telling property as a quiet glyph — its kind (computer, browser, remote), the
//   program it was imported from, a loop, a fork, private, the machine it runs on where that breaks
//   its group's rule, its subagents' number — or, inside a project, a quiet dot. Over the glyph sits
//   the chat's live state as a dot: needs you, working, failed, or a result not seen yet. A leader's
//   slot is the button that folds its subagents out, which hang under it on a tree line.
// - the title, cut with an ellipsis and whole in the tooltip;
// - one thing at the right edge: its state in a word while it asks for the reader or works ("needs
//   you" in the attention colour, "working" in the live one), else how many of its subagents work,
//   else when it last moved. Hovering or focusing the row puts its actions there instead.
// Everything the row does not draw — the kind in words, the machine, the loop's cadence, the fork's
// origin, the terminals, the model — is in its tooltip and its accessible name. The marks once stood
// in a row of pills between the title and the time, and an installation whose chats all run on the
// host drew the same amber "Host" on every row: a 276 px column left a title two words.
//
// Where chats run is said once, where it is the rule: a project whose chats run on the host says so
// by a lock on its own line, as does the Chats label for the chats outside projects, and only a chat
// that runs elsewhere than the rest of its group carries a mark. On a native installation everything
// runs on the host and nothing is said.
//
// A project is a folder with its count, or its state in a word while one of its chats asks or works.
// Projects start collapsed, the one holding the open chat opens itself, and a collapsed project still
// lists the chats that wait for the reader, because a closed folder must never hide a question.
//
// What the operator pins sits in a block of its own above both sections, newest pin first, and is not
// drawn again below it: a pinned project keeps its fold and its live chats, a pinned chat its row.
//
// The filters are one menu on the "Chats" label: which kind of chat, which state, the loops and the
// archive. They stood above the list as five rows of tabs and chips, which pushed the first project a
// quarter of the way down the window.
//
// The keyboard: `/` opens the search from anywhere outside a text field, the arrows walk the rows
// (left and right fold and unfold a project), Enter opens, Ctrl+Enter opens beside, F2 renames, E
// archives, P pins or unpins, and Delete deletes after the same confirmation the menu asks for.

import { CompanionFace } from "../companionavatar";
import { useContextActions } from "../ui/context-menu";
import { type ReactNode, memo, useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { api, Project, type ProjectRef, SessionList, SessionSummary } from "../api";
import { projectPath } from "../folders";
import { Skeleton, fmtInterval, statusWord } from "../ui/components";
import { type MenuItem, OverflowMenu, Sheet, confirmDialog, toast } from "../ui/dialogs";
import { Menu, isAction } from "../ui/primitives";
import { relTime, relTimeLong, untilShort } from "../format";
import { type Folder, type Row as RowModel, type SidebarDay, agentName, arrange, folderOpen, kindOf, liveRow, rememberFolder, sidebarSections } from "../grouping";
import { Icon, type IconName } from "../icons";
import { MoveSessionSheet, ProjectSettingsSheet, useEnvironments } from "../projects";
import { MakeProjectSheet, asChatCommand, dropChat, endChatDrag, makeProjectCommand, startChatDrag, takesDrop } from "../chatproject";
import { openNewProject } from "../project/NewProject";
import { openImport } from "../imports/ImportExplorer";
import { PlateMark, importedHarness } from "../imports/ImportedChat";
import { harnessMeta } from "../imports/model";
import { navigate, newAgentPath, parse, pathFor, sessionPath, useRoute } from "../router";
import { subagentPath } from "../subagents";
import { agentsListing } from "../mode";
import { invalidate, useQuery } from "../store";
import { useStreamUp } from "../events";
import { WindowedRows } from "../virtual";
import { errorText } from "../ui";
import { latinKey } from "../navigation";
import { insideTerminal } from "../terminal/keys";
import { plural, t } from "../i18n";
import { useReveal } from "../reveal";
import { pinCommand, setPinned } from "../pins";
import { PrivateMark, isPrivate } from "../privatechat";
import { type ChatKind, KIND_TABS, byTab, kindIcon, kindPlate, tabCounts, useKindTab } from "../chatkind";
import { remoteRowWords } from "../remote";

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

export function SessionsScreen({ onOpen, toast, current, project = "", projects = [], onProjects, onClearProject, searchOpen = false, onSearchOpen }: { onOpen: (id: string) => void; toast: (t: string) => void; current?: string; project?: string; projects?: Project[]; onProjects?: () => void; onClearProject?: () => void; searchOpen?: boolean; onSearchOpen?: (open: boolean) => void }) {
  const [view, setView] = useState<View>("all");
  // Which kind of chat the column shows (chatkind.tsx): remembered in this browser, and the mode New
  // chat starts in. A search looks through every kind, the way it looks past the status chips.
  const [tab, setTab] = useKindTab();
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
  // The pages fetched past the first are kept while the first page's end stays where it was. Reset on
  // every poll, as they were, the rows scrolled into would vanish each minute under the reader.
  const firstCursor = data?.next_cursor ?? null;
  useEffect(() => {
    setExtra([]);
    setNext(firstCursor);
  }, [firstCursor, view]);
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
  // `/` opens the search from anywhere a key is not already somebody's: Ctrl+K is the palette's,
  // and a focused terminal or text field keeps every letter. Heard on the window, after the
  // document: a screen with a search of its own (the calendar's `/`) takes the key there first and
  // marks it handled, and the column, mounted before that screen, used to steal it.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey || e.defaultPrevented) return;
      if (typing(e.target) || insideTerminal(e.target)) return;
      if (document.querySelector("[role=dialog], [role=menu]")) return;
      e.preventDefault();
      onSearchOpen?.(true);
      searchRef.current?.focus();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onSearchOpen]);
  // Opened from the head's button, the field takes the focus; closed, what it held is dropped, or the
  // list would stay narrowed to a search nobody can see.
  useEffect(() => {
    if (searchOpen) searchRef.current?.focus();
    else setQuery("");
  }, [searchOpen]);
  const lens = projects.find((p) => p.id === project);

  // The Agents list never shows what belongs to orchestration mode, found by a search or not.
  // A row a later page brought that has since moved up into the first page is drawn once.
  const raw = searching ? results : data ? { ...data, sessions: [...data.sessions, ...extra.filter((row) => !data.sessions.some((first) => first.id === row.id))], next_cursor: next } : data;
  const listing = useMemo(() => (raw ? agentsListing(raw) : raw), [raw]);
  const kindShown = searching ? "all" : tab;
  const sessions = useMemo(() => byTab(listing?.sessions ?? [], kindShown), [listing, kindShown]);
  const arranged = useMemo(
    () => arrange(sessions, listing?.projects ?? [], { project, results: searching, filter: view === "loops" ? "loops" : "all" }),
    [sessions, listing, project, searching, view],
  );
  // A kind tab is a filter like a status chip: a project is listed while it holds such a chat.
  const filtered = searching || view !== "all" || kindShown !== "all";
  // A chat's own project by its id, for "Make a project" on the chat's row: the day groups draw rows,
  // and a row has no other way to the project it would become.
  const chatProjects = useMemo(() => new Map(arranged.folders.filter((f) => f.chat).map((f) => [f.key, f.project])), [arranged]);
  // The archive is a list of what was put away, where nothing is lifted out of its order.
  const sections = useMemo(() => sidebarSections(arranged.folders, { filtered, pins: view !== "archive" }), [arranged, filtered, view]);
  // Where the chats outside projects run, said once on the Chats label; only on an installation in a
  // container, where a chat may run on either side.
  const docker = !!useEnvironments().data?.docker;
  const looseRule = useMemo(
    () => (docker ? envRule([...sections.pinned.filter((f) => f.chat).flatMap((f) => f.rows), ...sections.days.flatMap((d) => d.rows)]) : undefined),
    [docker, sections],
  );
  const counts = useMemo(() => {
    const top = (everything.data ? agentsListing(everything.data).sessions : []).filter((s) => !s.metadata?.subagent_of && (!project || s.project_id === project));
    return {
      all: top.length,
      attention: top.filter((s) => s.needs_attention || s.status === "waiting").length,
      working: top.filter((s) => kindOf(s, false) === "working").length,
      kinds: tabCounts(top),
    };
  }, [everything.data, project]);

  const list = useRef<HTMLDivElement>(null);
  // The rows sliding in when the tab changes say the list was replaced, not scrolled; skipped for a
  // reader who asked for less motion, and where the browser has no animations (the tests' DOM).
  const firstTab = useRef(true);
  useEffect(() => {
    if (firstTab.current) { firstTab.current = false; return; }
    const el = list.current;
    if (!el || typeof el.animate !== "function" || window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
    el.animate([{ opacity: 0.25, transform: "translateY(6px)" }, { opacity: 1, transform: "none" }], { duration: 200, easing: "cubic-bezier(.2,.7,.2,1)" });
  }, [tab]);
  // Past the first page the list goes on by itself: the "More" button loads the next page as soon as
  // it scrolls into sight, and stays a button for a keyboard and for a failed page.
  const more = useRef<HTMLButtonElement>(null);
  const fetchingMore = useRef(false);
  const loadMore = useCallback(async () => {
    if (!next || fetchingMore.current) return;
    fetchingMore.current = true;
    setPaging(true);
    try {
      const page = await api.get<SessionList>(`/api/sessions?view=${serverView}&cursor=${encodeURIComponent(next)}`);
      setExtra((held) => [...held, ...page.sessions.filter((row) => !held.some((old) => old.id === row.id))]);
      setNext(page.next_cursor ?? null);
    } catch (e) {
      // The button stays for a retry; the watcher does not fire again until it leaves sight and returns.
      toast(errorText(e));
    } finally {
      fetchingMore.current = false;
      setPaging(false);
    }
  }, [next, serverView, toast]);
  useEffect(() => {
    const button = more.current;
    if (!button || searching || typeof IntersectionObserver === "undefined") return;
    const watcher = new IntersectionObserver((entries) => { if (entries.some((entry) => entry.isIntersecting)) void loadMore(); });
    watcher.observe(button);
    return () => watcher.disconnect();
  }, [loadMore, searching, next]);
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

  // Escape anywhere in the column lifts a lens, the same key that clears the search. A field keeps
  // its own Escape, and an open menu or dialog closes first: those are drawn in a portal, whose keys
  // still bubble through React to here.
  const onColumnKey = (e: React.KeyboardEvent) => {
    if (e.key !== "Escape" || !lens || !onClearProject || e.defaultPrevented || typing(e.target)) return;
    if (document.querySelector("[role=dialog], [role=menu]")) return;
    e.preventDefault();
    onClearProject();
  };

  // The "Chats" label is the filters' menu: which kind of chat, which state, the loops, the archive.
  // Choices that set how the list reads keep the menu open, as a checkbox does.
  const kindItems: MenuItem[] = KIND_TABS.map((k) => ({
    label: t(k === "all" ? "side.show.all" : `kind.tab.${k}`), checked: tab === k, aside: everything.data ? <span className="num">{counts.kinds[k] ?? 0}</span> : undefined,
    onSelect: () => setTab(k),
  }));
  const chatsMenu: MenuItem[] = [
    { group: t("side.show") },
    ...kindItems,
    { group: t("side.state") },
    { label: t("agents.filter.all"), checked: view === "all", aside: everything.data ? <span className="num">{counts.all}</span> : undefined, onSelect: () => setView("all") },
    { label: t("side.filter.attention"), checked: view === "attention", aside: everything.data ? <span className="num">{counts.attention}</span> : undefined, onSelect: () => setView(view === "attention" ? "all" : "attention") },
    { label: t("side.filter.working"), checked: view === "working", aside: everything.data ? <span className="num">{counts.working}</span> : undefined, onSelect: () => setView(view === "working" ? "all" : "working") },
    "-",
    { label: t("ph.filter.loops"), icon: "loop", checked: view === "loops", onSelect: () => setView(view === "loops" ? "all" : "loops") },
    { label: t("agents.filter.archive"), icon: "archive", checked: view === "archive", onSelect: () => setView(view === "archive" ? "all" : "archive") },
  ];
  // What the label says while a filter is on, so a short list is never read as "my chats are gone".
  const viewWord = view === "attention" ? t("side.filter.attention") : view === "working" ? t("side.filter.working") : view === "loops" ? t("ph.filter.loops") : view === "archive" ? t("agents.filter.archive") : "";
  const filterWords = [tab !== "all" ? t(`kind.tab.${tab}`) : "", viewWord].filter(Boolean).join(" · ");
  const projectCount = sections.projects.length;
  const pinnedCount = sections.pinned.length;

  return (
    <>
      {(lens || searchOpen) && (
        <div className="sb-tools" onKeyDown={onColumnKey}>
          {/* The lens narrows every list in the column, and a quiet chip naming a project was all that
              said so: an operator who forgot it was on read a column of two chats as "my dialogs are
              gone". So a lens is announced in words with its count and lifted in one click. */}
          {lens && onClearProject && (
            <div className="sb-lens" role="status" title={t("side.lens.hint")}>
              <Icon name="folder" size={14} />
              <span className="truncate">{everything.data ? plural("side.lens.count", counts.all, { name: lens.name }) : t("side.lens.only", { name: lens.name })}</span>
              <button type="button" className="sb-lens-clear" onClick={onClearProject} aria-label={t("ph.lens.clear", { name: lens.name })}>
                {t("side.lens.showall")}<Icon name="close" size={12} />
              </button>
            </div>
          )}
          {searchOpen && (
            <label className={`sb-search ${query ? "filled" : ""}`}>
              <Icon name="search" size={16} />
              <input ref={searchRef} type="search" placeholder={t("side.search.placeholder")} value={query} maxLength={500} aria-label={t("search.label")}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Escape") { e.preventDefault(); if (query) setQuery(""); else onSearchOpen?.(false); }
                  if (e.key === "ArrowDown") { e.preventDefault(); list.current?.querySelector<HTMLElement>("[data-nav]")?.focus(); }
                }} />
              <button type="button" className="sb-clear" onClick={() => onSearchOpen?.(false)} aria-label={t("side.search.close")} title={t("side.search.close")}><Icon name="close" size={14} /></button>
            </label>
          )}
        </div>
      )}
      <div ref={list} className="screen agents-screen sb-list" onKeyDown={(e) => { onListKey(e); onColumnKey(e); }}>
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
        {pinnedCount > 0 && (
          <>
            <div className="sb-sec sb-pinsec"><span>{t("side.pinned")}</span></div>
            <div className="sb-pinned" data-pinned="">
              {sections.pinned.map((f) => f.chat
                ? <PinnedChat key={f.key} folder={f} onOpen={onOpen} current={current} rule={looseRule?.env} />
                : <ProjectGroup key={f.key} folder={f} onOpen={onOpen} current={current} filtered={filtered} toast={toast} docker={docker} />)}
            </div>
          </>
        )}
        {(!searching || projectCount > 0) && (
          <>
            {/* The label is the way to every project, archived ones included; the + makes one. */}
            <div className="sb-sec sb-sec-projects">
              {onProjects ? <button type="button" className="sb-sec-link" data-action="all-projects" onClick={onProjects} title={t("side.allprojects.title")}>{t("side.projects")}</button> : <span>{t("side.projects")}</span>}
              {!searching && <button type="button" className="iconbtn sb-sec-add" onClick={() => openNewProject()} aria-label={t("np.title")} title={t("np.title")}><Icon name="plus" size={14} /></button>}
            </div>
            {sections.projects.map((f) => (
              <ProjectGroup key={f.key} folder={f} onOpen={onOpen} current={current} filtered={filtered} toast={toast} docker={docker} />
            ))}
          </>
        )}
        {(!searching || sections.chats > 0) && (
          <div className="sb-sec sb-sec-chats">
            {searching ? <span>{t("side.chats")}</span> : (
              <Menu label={t("side.chats.menu")} side="bottom" align="start" items={chatsMenu} trigger={
                <button type="button" className={`sb-sec-link sb-chats-menu ${filterWords ? "on" : ""}`} aria-label={filterWords ? `${t("side.chats.menu")}: ${filterWords}` : t("side.chats.menu")}>
                  <span>{t("side.chats")}</span>
                  {filterWords && <span className="sb-sec-filter truncate">{filterWords}</span>}
                  <Icon name="chevron" size={12} />
                </button>
              } />
            )}
            {sections.chats > 0 && <EnvRuleMark rule={looseRule} />}
          </div>
        )}
        {!searching && data && kindShown !== "all" && view === "all" && arranged.shown === 0 ? (
          <div className="empty sb-kind-empty" data-kind-empty={kindShown}>
            {kindIcon(kindShown) && <span className={`sb-kind-empty-icon kind-${kindShown}`}><Icon name={kindIcon(kindShown)!} size={16} /></span>}
            <b>{t(`kind.empty.${kindShown}`)}</b>
            {kindShown !== "chat" && <div>{t(`kind.empty.${kindShown}.sub`)}</div>}
          </div>
        ) : ((results && !pending && arranged.shown === 0) || (!searching && data && filtered && arranged.shown === 0)) && <div className="empty sb-empty">{t("common.nothing")}</div>}
        {sections.days.map((d) => <DayGroup key={d.day} day={d.day} rows={d.rows} onOpen={onOpen} current={current} chatProjects={chatProjects} rule={looseRule?.env} />)}
        {!searching && next && <button ref={more} className="btn ghost load-more" disabled={paging} onClick={() => void loadMore()}>{t(paging ? "agents.loading" : "agents.more")}</button>}
      </div>
    </>
  );
}

/** One row per line a list draws: a chat, then the forks taken from it, then theirs, and under a chat
 *  whose helpers are unfolded, each of them (`sub`, with `last` on the one that ends the tree). The
 *  nesting is in the model and not in the DOM, so the rows can be windowed as one list. */
type Line = { row: RowModel; fork?: { of: string; seq: number }; sub?: SessionSummary; last?: boolean };

// Which chats have their subagents unfolded in the column. A convenience of this browser, kept per
// chat: the helpers used to be reachable only through the right-click menu, so the fold is offered on
// every row that has them and remembered, and a chat whose helper is open beside it unfolds itself.
const SUBS_KEY = "daedalus.side.subagents";
const subsListeners = new Set<() => void>();
let subsOpen: ReadonlySet<string> = readSubsOpen();

function readSubsOpen(): ReadonlySet<string> {
  try {
    const stored = JSON.parse(localStorage.getItem(SUBS_KEY) ?? "[]");
    return new Set(Array.isArray(stored) ? stored.filter((id): id is string => typeof id === "string") : []);
  } catch {
    return new Set();
  }
}

/** Fold or unfold one chat's subagents, and remember it. */
export function toggleSubagents(id: string, to?: boolean): void {
  const next = new Set(subsOpen);
  if (to ?? !next.has(id)) next.add(id);
  else next.delete(id);
  subsOpen = next;
  try {
    localStorage.setItem(SUBS_KEY, JSON.stringify([...next]));
  } catch {
    /* private mode: the fold lasts for the visit */
  }
  for (const listener of [...subsListeners]) listener();
}

function subscribeSubs(listener: () => void): () => void {
  subsListeners.add(listener);
  return () => {
    subsListeners.delete(listener);
  };
}

/** Whether a chat's subagents are drawn: unfolded by the reader, or holding the helper open beside it. */
function useSubsShown(): (row: RowModel) => boolean {
  const open = useSyncExternalStore(subscribeSubs, () => subsOpen, () => subsOpen);
  const route = useRoute();
  const sub = route.query.get("panel") === "subagents" ? route.query.get("sub") : null;
  return useCallback((row: RowModel) => row.kids.length > 0 && (open.has(row.s.id) || (!!sub && row.kids.some((k) => k.id === sub))), [open, sub]);
}

/** The lines with each unfolded chat's subagents under it. */
function withSubs(shown: Line[], unfolded: (row: RowModel) => boolean): Line[] {
  const out: Line[] = [];
  for (const line of shown) {
    out.push(line);
    if (line.sub || !unfolded(line.row)) continue;
    line.row.kids.forEach((kid, i) => out.push({ row: line.row, sub: kid, last: i === line.row.kids.length - 1 }));
  }
  return out;
}

const lineKey = (l: Line) => (l.sub ? `sub:${l.sub.id}` : l.row.s.id);

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

type EnvName = "host" | "container";
/** Where most of a group's chats run, and whether all of them do. */
type EnvRule = { env: EnvName; all: boolean };

function envOf(s: SessionSummary): EnvName {
  return s.env === "host" ? "host" : "container";
}

/** Where most of these chats run, forks included: the host when more than half of them run there.
 *  The container is what a Docker installation runs a chat in unless asked, so it wins a tie and is
 *  never said as a rule; a group without chats has no rule. */
function envRule(rows: RowModel[]): EnvRule | undefined {
  const all = lines(rows).map((l) => envOf(l.row.s));
  if (!all.length) return undefined;
  const host = all.filter((env) => env === "host").length;
  return host * 2 > all.length ? { env: "host", all: host === all.length } : { env: "container", all: host === 0 };
}

/** The rule said once, on a project's line or the Chats label: a lock while the group runs on the host. */
function EnvRuleMark({ rule }: { rule?: EnvRule }) {
  if (rule?.env !== "host") return null;
  const words = t(rule.all ? "side.env.rule.all" : "side.env.rule.most");
  return <span className="sb-env" data-env-rule={rule.all ? "all" : "most"} role="img" aria-label={words} title={words}><Icon name="lock" size={12} /></span>;
}

/** The live state a project's tile carries: a question first, then work. */
function folderState(rows: RowModel[]): "waiting" | "running" | "" {
  const all = lines(rows).flatMap((l) => [l.row.s, ...l.row.kids]);
  if (all.some((s) => s.status === "waiting")) return "waiting";
  if (all.some((s) => s.status === "running" || s.status === "compacting")) return "running";
  return "";
}

/** One day of the chats section, without a heading of its own: each row says when it last moved at its
 *  right edge, so the day's name was a row of its own saying the same. Windowed: a long history is the
 *  same few rows in the DOM. */
function DayGroup({ day, rows, onOpen, current, chatProjects, rule }: { day: SidebarDay; rows: RowModel[]; onOpen: (id: string) => void; current?: string; chatProjects: Map<string, ProjectRef>; rule?: EnvName }) {
  const host = useRef<HTMLDivElement>(null);
  const unfolded = useSubsShown();
  const shown = useMemo(() => withSubs(lines(rows), unfolded), [rows, unfolded]);
  const keys = useMemo(() => shown.map(lineKey), [shown]);
  return (
    <div ref={host} className="sb-day" data-day={day}>
      <WindowedRows keys={keys} host={host} rowSelector=":scope > .sb-row" estimate={34} render={(i) => shown[i].sub ? (
        <SubagentRow kid={shown[i].sub!} leader={shown[i].row.s} last={!!shown[i].last} />
      ) : (
        <ChatRow row={shown[i].row} fork={shown[i].fork} onOpen={onOpen} current={current} projectName={shown[i].row.s.project} pin={shown[i].fork ? undefined : "off"} subsOpen={unfolded(shown[i].row)} chat={shown[i].fork ? undefined : chatProjects.get(shown[i].row.s.project_id)} envRule={rule} />
      )} />
    </div>
  );
}

/** A pinned chat in the pinned block: its row and the forks taken from it, as under its day. Not
 *  windowed: a pin is a handful of rows by intent. */
function PinnedChat({ folder, onOpen, current, rule }: { folder: Folder; onOpen: (id: string) => void; current?: string; rule?: EnvName }) {
  const unfolded = useSubsShown();
  const shown = useMemo(() => withSubs(lines(folder.rows), unfolded), [folder.rows, unfolded]);
  return <>{shown.map((l) => l.sub
    ? <SubagentRow key={lineKey(l)} kid={l.sub} leader={l.row.s} last={!!l.last} />
    : <ChatRow key={lineKey(l)} row={l.row} fork={l.fork} onOpen={onOpen} current={current} projectName={l.row.s.project} pin={l.fork ? undefined : "on"} subsOpen={unfolded(l.row)} chat={l.fork ? undefined : folder.project} envRule={rule} />)}</>;
}

type GroupProps = { folder: Folder; onOpen: (id: string) => void; current?: string; filtered: boolean; toast: (text: string) => void; docker: boolean };

/** Whether a project's group would look the same. The listing is re-fetched whole and every object in
 *  it is new each time; `sig` is built once while the folders are arranged, so this costs a string
 *  comparison where a reconcile of every row would cost the frame. */
function sameGroup(a: GroupProps, b: GroupProps): boolean {
  const l = a.folder, r = b.folder;
  if (a.filtered !== b.filtered || a.current !== b.current || a.onOpen !== b.onOpen || a.toast !== b.toast || a.docker !== b.docker) return false;
  if (l.key !== r.key || l.name !== r.name || l.total !== r.total || l.system !== r.system || l.last_message_at !== r.last_message_at || l.pinned_at !== r.pinned_at) return false;
  if (projectPath(l.project) !== projectPath(r.project) || l.project.entity_revision !== r.project.entity_revision) return false;
  return l.sig === r.sig;
}

/**
 * A project: a one-line row with a square tile and its count, and its chats on a guide line under it
 * once it is open. Collapsed is the default and is remembered per project; the project holding the
 * open chat opens itself without remembering it, and a search or a filter opens what it found.
 * Collapsed, it still lists its live chats (waiting, working, failed).
 */
const ProjectGroup = memo(function ProjectGroup({ folder, onOpen, current, filtered, toast, docker }: GroupProps) {
  const [open, setOpen] = useState(() => folderOpen(folder.key));
  const [all, setAll] = useState(false);
  const [editing, setEditing] = useState(false);
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
  const newIn = useCallback(() => navigate(newAgentPath(folder.key)), [folder.key]);
  const revealer = useReveal();
  const pinned = !!folder.pinned_at;
  const backToChat = asChatCommand(folder);
  const [dropping, setDropping] = useState(false);
  const items: MenuItem[] = [
    { label: t("side.project.newagent", { name: folder.name }), icon: "compose", onSelect: newIn },
    pinCommand(folder.key, pinned, { key: true }),
    ...(folder.system ? [{ label: t("agents.folder.tocompanion"), icon: "mic" as const, onSelect: () => navigate(pathFor("companion")) }] : []),
    { label: t("project.settings.for", { name: folder.name }), icon: "settings", onSelect: editProject },
    ...(backToChat ? [backToChat] : []),
    ...(revealer ? [{ label: revealer.label, icon: "external" as const, onSelect: () => void revealer.reveal({ project_id: folder.key }) }] : []),
    { label: t(showing ? "agents.folder.hide" : "agents.folder.show", { name: folder.name }), icon: "chevron", onSelect: () => toggle() },
  ];
  useContextActions(head, items);
  const all_lines = useMemo(() => lines(folder.rows), [folder.rows]);
  // Only the lines that wait for the operator: a working chat is said by the project's own word, and
  // listing it as well made a closed folder as long as an open one. An idle fork of a waiting chat is
  // not what the collapsed row keeps in sight for.
  const peek = useMemo(() => all_lines.filter((l) => liveRow(l.row) && rowState(l.row.s, l.row.kids) === "waiting"), [all_lines]);
  const chats = showing ? (all ? all_lines : all_lines.slice(0, KIDS_SHOWN)) : peek;
  const hidden = showing ? all_lines.length - chats.length : 0;
  const unfolded = useSubsShown();
  const shown = useMemo(() => withSubs(chats, unfolded), [chats, unfolded]);
  const keys = useMemo(() => shown.map(lineKey), [shown]);
  const state = folderState(folder.rows);
  // Where the project's chats run, said on its line when it is the host; a chat that runs elsewhere
  // than the rest says so in its own slot.
  const rule = useMemo(() => (docker ? envRule(folder.rows) : undefined), [docker, folder.rows]);
  const ruleWords = rule?.env === "host" ? t(rule.all ? "side.env.rule.all" : "side.env.rule.most") : "";
  const onKey = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.target !== e.currentTarget) return;
    const plain = !e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey;
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
    else if (latinKey(e) === "p" && plain) { e.preventDefault(); void setPinned(folder.key, !pinned); }
    else if (e.key === "ArrowRight") {
      e.preventDefault();
      if (!showing) toggle(true);
      else section.current?.querySelector<HTMLElement>(".sb-kids [data-nav]")?.focus();
    } else if (e.key === "ArrowLeft" && showing) { e.preventDefault(); toggle(false); }
  };
  return (
    <section ref={section} data-project={folder.key} className={`sb-project ${showing ? "open" : ""} ${folder.system ? "system" : ""}`}>
      {/* A chat dragged onto the row moves into the project; the keyboard and the phone use Move. */}
      <div ref={head} className={`sb-row sb-prow ${dropping ? "drop" : ""}`} data-nav="project" data-project-head="" role="button" tabIndex={0} aria-expanded={showing}
        aria-label={[t(showing ? "agents.folder.hide" : "agents.folder.show", { name: folder.name }), ruleWords].filter(Boolean).join(", ")} title={[projectPath(folder.project), ruleWords].filter(Boolean).join("\n")}
        onClick={() => toggle()} onKeyDown={onKey}
        onDragOver={(e) => { if (!takesDrop(e, folder)) return; e.preventDefault(); e.dataTransfer.dropEffect = "move"; if (!dropping) setDropping(true); }}
        onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDropping(false); }}
        onDrop={(e) => { setDropping(false); if (!takesDrop(e, folder)) return; e.preventDefault(); toggle(true); void dropChat(folder); }}>
        <span className="sb-tile">{folder.system ? <CompanionFace size={16} /> : <Icon name="folder" size={16} />}</span>
        <span className="sb-t">{folder.name}</span>
        <EnvRuleMark rule={rule} />
        <span className="sb-ptrail">
          {showing ? <span className="sb-chev" aria-hidden><Icon name="chevron" size={14} /></span>
            : state ? <span className={`sb-word ${state}`}>{stateWord(state)}</span>
            : folder.total > 0 && <span className="sb-pcount num" title={plural("agents.count", folder.total)}>{folder.total}</span>}
        </span>
        <span className="sb-acts" onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
          <button type="button" className="iconbtn small quiet sb-padd" onClick={newIn} aria-label={t("side.project.newagent", { name: folder.name })} title={t("side.project.newagent", { name: folder.name })}><Icon name="plus" size={14} /></button>
          <OverflowMenu small className="quiet" label={t("side.project.menu", { name: folder.name })} items={items} />
        </span>
      </div>
      {(shown.length > 0 || (showing && folder.rows.length === 0)) && (
        <div className={`sb-kids ${showing ? "" : "peek"}`}>
          {showing && folder.rows.length === 0 && <div className="sb-empty sub">{filtered ? t("common.nothing") : t("agents.folder.none")}</div>}
          <WindowedRows keys={keys} host={section} rowSelector=":scope > .sb-kids > .sb-row" estimate={34} render={(i) => shown[i].sub ? (
            <SubagentRow kid={shown[i].sub!} leader={shown[i].row.s} last={!!shown[i].last} nested />
          ) : (
            <ChatRow row={shown[i].row} fork={shown[i].fork} nested onOpen={onOpen} current={current} projectName={folder.name} onNewIn={newIn} onParent={() => head.current?.focus()} subsOpen={unfolded(shown[i].row)} envRule={rule?.env} />
          )} />
          {(hidden > 0 || (all && all_lines.length > KIDS_SHOWN)) && (
            <button type="button" className="sb-more" onClick={() => setAll((v) => !v)}>
              {hidden > 0 ? t("side.more", { n: hidden }) : t("side.fewer")}
            </button>
          )}
        </div>
      )}
      {editing && <ProjectSettingsSheet project={{ ...folder.project, sessions: folder.rows.map((r) => ({ id: r.s.id, title: r.s.title })) }} onClose={() => setEditing(false)} onRemoved={() => setEditing(false)} toast={toast} />}
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
  /** Inside a project's fold: a quiet dot in the slot when the chat has no glyph of its own. */
  nested?: boolean;
  projectName: string;
  onNewIn?: () => void;
  /** Where Left goes from a chat inside a project: to the project's row. */
  onParent?: () => void;
  /** Whether the row is a chat that can be pinned (its scratch project is), and whether it is: a chat
   *  inside a project is not pinned on its own, its project is. */
  pin?: "on" | "off";
  /** Whether the chat's subagents are unfolded under it. */
  subsOpen?: boolean;
  /** The chat's own project, on the row of a chat that is not in a project: "Make a project" settles it. */
  chat?: ProjectRef;
  /** Where most chats of the row's group run, on an installation where it can differ: the row marks
   *  itself only when it runs elsewhere. Absent where every chat runs on the host. */
  envRule?: EnvName;
};

/** What a row actually draws, so a fresh read of the list does not reconcile a row that did not
 *  change. The children are in it because their state is the leader's; the unread mark because a
 *  run that finishes unseen changes nothing else a row shows. */
function sameRow(a: RowProps, b: RowProps): boolean {
  const l = a.row.s, r = b.row.s;
  if (l.id !== r.id || l.title !== r.title || l.status !== r.status || !!l.unread_result !== !!r.unread_result || l.last_message_at !== r.last_message_at || l.model !== r.model || l.env !== r.env || !!l.archived !== !!r.archived || l.kind !== r.kind) return false;
  if (remoteRowWords(l) !== remoteRowWords(r) || (l.controlling ?? []).join() !== (r.controlling ?? []).join() || isPrivate(l) !== isPrivate(r) || l.imported_from !== r.imported_from) return false;
  if ((l.terminals ?? 0) !== (r.terminals ?? 0) || l.match?.snippet !== r.match?.snippet || l.project_id !== r.project_id) return false;
  if (a.projectName !== b.projectName || a.onNewIn !== b.onNewIn || a.onOpen !== b.onOpen || a.envRule !== b.envRule) return false;
  if ((a.current === l.id || a.row.kids.some((k) => k.id === a.current)) !== (b.current === r.id || b.row.kids.some((k) => k.id === b.current))) return false;
  if (a.nested !== b.nested || a.fork?.seq !== b.fork?.seq || a.fork?.of !== b.fork?.of || a.pin !== b.pin || !!a.subsOpen !== !!b.subsOpen) return false;
  if (a.chat?.id !== b.chat?.id || a.chat?.entity_revision !== b.chat?.entity_revision || !!a.chat?.settings.ephemeral !== !!b.chat?.settings.ephemeral) return false;
  if (JSON.stringify(l.metadata?.loop ?? null) !== JSON.stringify(r.metadata?.loop ?? null)) return false;
  const lk = a.row.kids, rk = b.row.kids;
  return lk.length === rk.length && lk.every((k, i) => k.id === rk[i].id && k.status === rk[i].status && k.title === rk[i].title);
}

/** The state a project's row says in place of its count, and the collapsed project keeps a chat in
 *  sight for: a question anywhere in the chat's tree, its failure, or work in it. */
function rowState(s: SessionSummary, kids: SessionSummary[]): "waiting" | "running" | "failed" | "" {
  if (s.status === "waiting" || kids.some((k) => k.status === "waiting")) return "waiting";
  if (s.status === "failed") return "failed";
  if (s.status === "running" || s.status === "compacting" || kids.some((k) => k.status === "running")) return "running";
  return "";
}

/** What a chat's own row says in a word at its right edge: a question anywhere in its tree, its own
 *  failure, its own work. Its subagents at work are said by their number there instead, which is
 *  what the reader unfolds them for. */
function wordState(s: SessionSummary, kids: SessionSummary[]): "waiting" | "running" | "failed" | "" {
  if (s.status === "waiting" || kids.some((k) => k.status === "waiting")) return "waiting";
  if (s.status === "failed") return "failed";
  return s.status === "running" || s.status === "compacting" ? "running" : "";
}

/** The word for a row's state, lower case as it trails the title: "needs you", "working", "failed". */
function stateWord(state: "waiting" | "running" | "failed"): string {
  return t(`side.state.${state}`);
}

const working = (s: SessionSummary) => s.status === "running" || s.status === "compacting";

/** The one property a chat's slot draws, the most telling first: what the chat is (a computer, a
 *  browser, a remote terminal, a program it came from), that it runs by itself, where it came from,
 *  that it is private, that it runs elsewhere than its group, that it was put away, and for a plain
 *  leader how many helpers it has. */
type Glyph = { key: string; node: ReactNode };

function glyphOf(s: SessionSummary, o: { kind: ChatKind; kindClass: string; kindTitle?: string; imported: string; fork: boolean; envOff: boolean; kids: number }): Glyph | null {
  const quiet = (key: string, icon: IconName, words: string): Glyph => ({ key, node: <span className={`sb-glyph g-${key}`} role="img" aria-label={words} title={words}><Icon name={icon} size={14} /></span> });
  const kindGlyph = kindIcon(o.kind);
  if (kindGlyph) {
    return { key: "kind", node: <span className={`sb-kind${o.kindClass}`} data-kind={o.kind} title={o.kindTitle} role="img" aria-label={o.kindTitle}><Icon name={kindGlyph} size={14} /></span> };
  }
  if (o.imported) return { key: "imported", node: <PlateMark harness={o.imported} /> };
  if (s.metadata?.loop) return quiet("loop", "loop", loopLine(s));
  if (o.fork) return quiet("fork", "fork", t("agents.fork.at", { n: s.metadata?.forked_from?.seq ?? 0 }));
  if (isPrivate(s)) return { key: "private", node: <PrivateMark on size={14} /> };
  if (o.envOff) return quiet(`env-${envOf(s)}`, envOf(s) === "host" ? "lock" : "terminal", t(`side.row.env.${envOf(s)}`));
  if (s.archived) return quiet("archived", "archive", t("side.row.archived"));
  if (o.kids) return { key: "subs", node: <span className="sb-subs-n num" aria-hidden>{o.kids}</span> };
  return null;
}

/**
 * A chat: its slot, its title, and at the right edge its state, its working helpers or its time.
 * Hovering or focusing it shows "Open beside" and the menu over the right edge; the right click opens
 * the same menu, with the commands of the phone's sheet. A leader's slot folds its helpers.
 */
const ChatRow = memo(function ChatRow({ row, onOpen, current, fork, nested, projectName, onNewIn, onParent, pin, subsOpen, chat, envRule: rule }: RowProps) {
  const s = row.s;
  const kids = row.kids;
  // The project's own settings are the project row's to offer; in a chat's menu they were one more
  // entry about something other than the chat clicked. A chat made a project is opened, so the
  // project it now heads unfolds around it, selected.
  const { items, layers, run } = useSessionCommands(s, { projectName, chat, onMadeProject: () => onOpen(s.id) });
  const here = current === s.id || kids.some((k) => k.id === current);
  const unread = !!s.unread_result;
  const state = wordState(s, kids);
  const helpersWorking = kids.filter(working).length;
  const imported = importedHarness(s);
  // A computer or Chrome chat says so in its slot, the mark that lets the mixed list of every kind, or
  // a search, be read without opening the chat. An ordinary chat holding the reader's Chrome or
  // computer now borrows that mark, hollow, until it lets go (chatkind.tsx).
  const plate = kindPlate(s);
  const kind = plate.kind;
  const env = envOf(s);
  const glyph = glyphOf(s, { kind, kindClass: plate.className ? ` ${plate.className}` : "", kindTitle: plate.title, imported, fork: !!(fork || s.metadata?.forked_from), envOff: !!rule && env !== rule, kids: kids.length });
  // The live state over the glyph, so it reads at the left edge while the eye runs down the titles:
  // a question first, then a failure, then work, then a result not seen yet.
  const pip = state === "waiting" || state === "failed" ? state : state === "running" || helpersWorking ? "running" : unread ? "unread" : "";
  const open = () => onOpen(s.id);
  const beside = () => {
    const route = parse();
    if (route.session && route.session !== s.id) navigate(sessionPath(route.session, s.id));
    else open();
  };
  // No entry per subagent: a chat with many helpers made the menu taller than the screen, and the
  // helpers are one click away already, in the slot that folds them out under the row.
  const menu: MenuItem[] = [
    { label: t("common.open"), icon: "open", hint: t("side.key.open"), onSelect: open },
    { label: t("side.beside"), icon: "columns", hint: t("side.key.beside"), onSelect: beside },
    { label: t("nav.ctx.newtab"), icon: "external", onSelect: () => void window.open(new URL(sessionPath(s.id), location.href).href, "_blank", "noopener") },
    ...(pin ? [pinCommand(s.project_id, pin === "on", { key: true })] : []),
    ...(onNewIn ? [{ label: t("side.project.newagent", { name: projectName }), icon: "compose" as const, onSelect: onNewIn }] : []),
    "-",
    ...items.map((item): MenuItem => {
      if (!isAction(item)) return item;
      const hint = item.icon === "pen" ? t("side.key.rename") : item.icon === "archive" ? t("side.key.archive") : item.icon === "trash" ? t("side.key.delete") : undefined;
      return hint ? { ...item, hint } : item;
    }),
  ];
  const withRule = menu.flatMap((item, i) => (isAction(item) && item.danger && menu[i - 1] !== "-" ? ["-" as const, item] : [item]));
  const onKey = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.target !== e.currentTarget) return;
    const plain = !e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey;
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); beside(); }
    else if ((e.key === "Enter" || e.key === " ") && plain) { e.preventDefault(); open(); }
    else if (e.key === "F2" && plain) { e.preventDefault(); run.rename(); }
    else if (latinKey(e) === "e" && plain) { e.preventDefault(); void run.archive(); }
    else if (latinKey(e) === "p" && plain && pin) { e.preventDefault(); void setPinned(s.project_id, pin === "off"); }
    else if (e.key === "Delete" && plain) { e.preventDefault(); void run.remove(); }
    else if (e.key === "ArrowRight" && plain && kids.length && !subsOpen) { e.preventDefault(); toggleSubagents(s.id, true); }
    else if (e.key === "ArrowLeft" && plain && kids.length && subsOpen) { e.preventDefault(); toggleSubagents(s.id, false); }
    else if (e.key === "ArrowLeft" && plain && onParent) { e.preventDefault(); onParent(); }
  };
  // Everything about the chat in words, whatever the slot and the right edge chose to draw: the
  // tooltip for a pointer and the row's name for a screen reader, which hears no glyph.
  const name = agentName(s);
  const forkOf = fork ? t("side.row.fork", { name: fork.of, n: fork.seq }) : s.metadata?.forked_from ? t("agents.fork.at", { n: s.metadata.forked_from.seq }) : "";
  const facts = [
    state ? stateWord(state) : "",
    unread ? t("agents.unread") : "",
    plate.title ?? "",
    imported ? t("imp.mark.title", { name: harnessMeta(imported).name }) : "",
    rule ? t(`side.row.env.${env}`) : "",
    remoteRowWords(s),
    s.metadata?.loop ? loopLine(s) : "",
    forkOf,
    isPrivate(s) ? t("private.mark") : "",
    kids.length ? (helpersWorking ? t("side.row.subs.working", { all: plural("side.row.subs", kids.length), n: helpersWorking }) : plural("side.row.subs", kids.length)) : "",
    s.terminals ? plural("term.count", s.terminals) : "",
    s.archived ? t("side.row.archived") : "",
    s.workspace_own && !fork ? t("agents.own.chip") : "",
    s.metadata?.subagent_of ? t("agents.orphan") : "",
    s.model ?? "",
    relTimeLong(s.last_message_at),
  ].filter(Boolean);
  const slotClass = `sb-gut${glyph ? ` g-${glyph.key}` : nested ? " g-nest" : " bare"}`;
  const slotInside = <>
    {glyph ? glyph.node : nested ? <span className="sb-nest" aria-hidden /> : null}
    {pip === "unread"
      ? <span className="unread-dot" aria-hidden />
      : pip && <span className={`sb-pip ${pip}`} aria-hidden />}
  </>;
  return (
    <div data-session={s.id} data-nav="chat" className={`sb-row sb-chat ${here ? "current" : ""} ${unread ? "unread" : ""} ${nested ? "nested" : ""} ${fork ? "fork" : ""} ${s.match ? "match" : ""}`}
      title={`${name}\n${facts.join(" · ")}`} aria-label={`${name}, ${facts.join(", ")}`} role="link" aria-current={here ? "page" : undefined} tabIndex={0} onClick={open} onKeyDown={onKey}
      draggable onDragStart={(e) => startChatDrag(e, s)} onDragEnd={endChatDrag}>
      {kids.length > 0 ? (
        // A leader's helpers fold out under it from its slot, the way a tree's node opens: the glyph
        // gives way to the chevron under the pointer, the keyboard's focus and while they are out.
        <button type="button" className={`${slotClass} sb-fold`} aria-expanded={!!subsOpen}
          aria-label={t(subsOpen ? "side.subs.hide" : "side.subs.show", { name })} title={t(subsOpen ? "side.subs.hide" : "side.subs.show", { name })}
          onClick={(e) => { e.stopPropagation(); toggleSubagents(s.id); }} onKeyDown={(e) => e.stopPropagation()}>
          {slotInside}
          <span className="sb-fold-chev" aria-hidden><Icon name="chevron" size={14} /></span>
        </button>
      ) : <span className={slotClass}>{slotInside}</span>}
      {s.match ? (
        <span className="sb-main">
          <span className="sb-t">{name}</span>
          <span className="sb-snip search-passage">{s.match.snippet}</span>
        </span>
      ) : <span className="sb-t">{name}</span>}
      {state
        ? <span className={`sb-word ${state}`}>{stateWord(state)}</span>
        : helpersWorking
          ? <span className="sb-subs live num" title={t("side.row.subs.working", { all: plural("side.row.subs", kids.length), n: helpersWorking })}><Icon name="spawn" size={12} />{helpersWorking}/{kids.length}</span>
          : <span className="sb-time num" title={new Date(s.last_message_at).toLocaleString()}>{relTime(s.last_message_at)}</span>}
      <span className="sb-acts session-row-menu" onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
        <button type="button" className="iconbtn small quiet" onClick={beside} aria-label={t("side.beside")} title={t("side.beside")}><Icon name="columns" size={14} /></button>
        <OverflowMenu contextSelector="[data-session]" small className="quiet" label={`${s.title}: ${t("dlg.menu")}`} items={withRule} />
        {layers}
      </span>
    </div>
  );
}, sameRow);

/** The dot a helper's row carries: working, waiting on a question, failed, or at rest. */
function subState(s: SessionSummary): "running" | "waiting" | "failed" | "idle" {
  if (s.status === "waiting") return "waiting";
  if (s.status === "running" || s.status === "compacting") return "running";
  return s.status === "failed" ? "failed" : "idle";
}

/**
 * A subagent under its unfolded leader: a quieter line whose slot is the tree line from the leader's,
 * with the helper's state as a dot on it, and its title where every title starts. It opens beside
 * the leader, in the Subagents tab; its full chat is the tab's button and the row's menu. The arrows
 * walk it like any row, and Left goes to the leader.
 */
const SubagentRow = memo(function SubagentRow({ kid, leader, last, nested }: { kid: SessionSummary; leader: SessionSummary; last: boolean; nested?: boolean }) {
  const route = useRoute();
  const here = route.session === leader.id && route.query.get("panel") === "subagents" && route.query.get("sub") === kid.id;
  const state = subState(kid);
  const box = useRef<HTMLDivElement>(null);
  const open = () => navigate(subagentPath(leader.id, kid.id));
  const full = () => navigate(sessionPath(kid.id));
  useContextActions(box, [
    { label: t("side.sub.panel"), icon: "spawn", onSelect: open },
    { label: t("subagents.full"), icon: "expand", onSelect: full },
  ]);
  const onKey = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.target !== e.currentTarget || e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return;
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); box.current?.parentElement?.querySelector<HTMLElement>(`[data-session="${CSS.escape(leader.id)}"]`)?.focus(); }
  };
  const name = agentName(kid);
  const facts = [state !== "idle" ? stateWord(state) : relTimeLong(kid.last_message_at), kid.model ?? ""].filter(Boolean);
  return (
    <div ref={box} data-subagent={kid.id} data-nav="chat" role="link" tabIndex={0} aria-current={here ? "page" : undefined} aria-label={`${name}, ${facts.join(", ")}`}
      className={`sb-row sb-sub ${here ? "current" : ""} ${last ? "last" : ""} ${nested ? "nested" : ""}`} title={kid.model ? `${kid.model} · ${kid.id}` : kid.id} onClick={open} onKeyDown={onKey}>
      <span className={`sb-gut sb-tree ${last ? "last" : ""}`} aria-hidden><span className={`sub-dot ${state}`} /></span>
      <span className="sb-t">{name}</span>
      {state !== "idle" ? <span className={`sb-word ${state}`}>{stateWord(state)}</span> : <span className="sb-time num">{relTime(kid.last_message_at)}</span>}
    </div>
  );
});

/**
 * A session's commands, for every place that offers them: the desktop row's ⋮ menu and the phone's
 * long-press sheet list the same items and open the same sheets, so a command added here reaches both.
 * `layers` are the sheets the commands open, to be rendered beside whatever shows the items.
 */
export function useSessionCommands(session: SessionSummary, opts: { onProject?: () => void; projectName?: string; chat?: ProjectRef | null; onMadeProject?: () => void } = {}): { items: MenuItem[]; layers: ReactNode; run: { rename: () => void; archive: () => Promise<void>; remove: () => Promise<void> } } {
  const [editing, setEditing] = useState(false);
  const [moving, setMoving] = useState(false);
  const [promoting, setPromoting] = useState(false);
  // Offered beside Move, the other way out of being a lone chat: `chat` is the session's own project
  // while it is still a chat's, passed only for a chat's own row (a fork's row shares the project).
  const makeProject = makeProjectCommand(opts.chat, () => setPromoting(true));
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
    ...(makeProject ? [makeProject] : []),
    ...(opts.onProject ? [{ label: t("project.settings.for", { name: opts.projectName ?? session.project }), icon: "settings" as const, onSelect: opts.onProject }] : []),
    { label: t(session.archived ? "agents.restore" : "agents.archive"), icon: "archive", onSelect: () => void archive() },
    { label: t("common.delete"), icon: "trash", danger: true, onSelect: () => void remove() },
  ];
  const layers = <>
    {moving && <MoveSessionSheet sessionId={session.id} current={session.project_id} currentOwn={!!session.workspace_own} onClose={() => setMoving(false)} onMoved={() => invalidate("/api/sessions")} toast={toast} />}
    {promoting && opts.chat && <MakeProjectSheet project={opts.chat} title={session.title} onClose={() => setPromoting(false)} onMade={opts.onMadeProject} />}
    {editing && <Sheet title={t("session.rename")} onClose={() => setEditing(false)} size="sm">
      <form onSubmit={(event) => { event.preventDefault(); void rename(); }}>
        <input className="field" aria-label={t("session.rename")} autoFocus value={title} maxLength={128} onChange={(event) => setTitle(event.target.value)} />
        <button className="btn primary" type="submit" disabled={saving || !title.trim()}>{t("common.save")}</button>
      </form>
    </Sheet>}
  </>;
  return { items, layers, run: { rename: startRename, archive, remove } };
}

export function useSessionTitles(): Record<string, string> {
  const { data } = useQuery<SessionList>("/api/sessions", { staleMs: 15000 });
  return useMemo(() => Object.fromEntries((data?.sessions ?? []).map((s) => [s.id, s.title])), [data]);
}
