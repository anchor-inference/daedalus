// The Terminals screen. Its Overview is every terminal on the machine as a card with the last rows of
// its screen, by project, with the load bar in the header and a way to open a free one. The cards are
// read from the host's listing (`?preview=6`), never from a connection: forty cards must not hold
// forty sockets. Its Workspace (TerminalWorkspace.tsx) is the terminals the operator set up, live, in
// a grid; opening a card shows that terminal there (`/app/terminals/<id>`).
//
// Which of the two `/app/terminals` opens on is `openingView`'s rule: the Workspace when panes are
// set up, unless the operator chose the Overview last.

import { useContextActions } from "../ui/context-menu";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useEdgeFade } from "../edgefade";
import { api, Project, SessionList, TerminalEnv, TerminalEnvName, TerminalList, TerminalLoad, TerminalView as TerminalRow } from "../api";
import { Skeleton } from "../ui/index";
import { primaryFolder } from "../folders";
import { plural, t } from "../i18n";
import { Icon } from "../icons";
import { LoadBar } from "../loadbar";
import { navigate, pathFor } from "../router";
import { PageHeader, screenTitle, useMedia } from "../ui/index";
import { PhoneTerminals } from "./TerminalsPhone";
import { TerminalWorkspaceScreen } from "./TerminalWorkspace";
import { StateDot, ViewSwitch } from "../terminal/workspace";
import { paneState } from "../terminal/status";
import { useTerminalCommands } from "../terminal/rowmenu";
import { loadView, loadWorkspace, openingView, saveView } from "../terminal/workspacestate";
import { invalidate, peek, useQuery } from "../store";
import { errorText } from "../ui";
import { createTerminalConfirmed, endTerminals, removeTerminals } from "../terminal/actions";
import { TerminalPicker } from "../terminal/picker";
import { EnvPill } from "../envpill";
import { cardStatus, FILTERS, groupTerminals, headerCounts, idleUnwatched, matches, ownerLine, previewRows, runStyle, running, staleFor, TerminalFilter } from "../terminal/preview";
import { TerminalStatusLine } from "../terminal/statusline";

/** How often the load bar asks again: the daemon measures at most every ten seconds anyway. */
const LOAD_POLL_MS = 10000;
export const LIST_KEY = "/api/terminals?preview=6";

/** The address of a terminal's full-screen view. */
export function terminalPath(id: string): string {
  return pathFor("terminals", id);
}

/** Where a new container terminal starts: the project the operator is looking at, else the folder of the conversation they used last. */
export function containerFolder(project: Project | null, sessions: SessionList["sessions"]): { path: string; projectId: string | null } | null {
  if (project) {
    const inside = project.folders.find((f) => f.env === "container" && f.reachable) ?? (primaryFolder(project)?.env === "container" ? primaryFolder(project) : undefined);
    if (inside) return { path: inside.path, projectId: project.id };
  }
  const recent = [...sessions].filter((s) => s.workspace_path).sort((a, b) => Date.parse(b.last_message_at || "") - Date.parse(a.last_message_at || ""))[0];
  return recent?.workspace_path ? { path: recent.workspace_path, projectId: recent.project_id || null } : null;
}

/** Opens a free terminal (asking at the cap) and goes to it; the shared path for this screen and the palette. */
export async function openFreeTerminal(env: TerminalEnvName, cwd: string | undefined, projectId: string | null, toast: (text: string) => void, sandbox = false): Promise<void> {
  let row: TerminalRow | null;
  try {
    row = await createTerminalConfirmed({ env, owner_kind: "free", cwd: cwd || undefined, project_id: projectId || undefined, ...(sandbox ? { sandbox: true } : {}) });
  } catch (error) {
    toast(errorText(error));
    return;
  }
  if (!row) return;
  invalidate("/api/terminals");
  navigate(terminalPath(row.id));
}

/** The "where?" question for a free terminal, on this screen, its Workspace and a phone: the folder
 *  the operator works in inside the container, their home on the host, or a folder they choose. */
export function FreeTerminalPicker({ envs, lens, projects, toast, anchor, phone, onClose }: { envs: TerminalEnv[]; lens: Project | null; projects: Project[]; toast: (text: string) => void; anchor: HTMLElement | null; phone: boolean; onClose: () => void }) {
  const sessions = peek<SessionList>("/api/sessions")?.sessions ?? [];
  const here = containerFolder(lens, sessions);
  const home = envs.find((e) => e.env === "host")?.home || null;
  return (
    <TerminalPicker
      envs={envs}
      folders={{ container: { path: here?.path ?? null, projectId: here?.projectId ?? null }, host: { path: home } }}
      projects={projects}
      lens={lens}
      anchor={anchor}
      phone={phone}
      onPick={(picked) => openFreeTerminal(picked.env, picked.cwd, picked.projectId ?? null, toast, picked.sandbox)}
      onClose={onClose}
    />
  );
}

export function TerminalsScreen(props: { detail: string | null; query: URLSearchParams; toast: (text: string) => void; project: string; projects: Project[] }) {
  const { detail, query, ...rest } = props;
  // A phone draws the same listing from its own pieces (TerminalsPhone.tsx): a capacity line, cards,
  // sheets; a terminal opened from it is the Workspace's one pane at a time.
  const phone = !useMedia("(min-width: 1024px)");
  const asked = query.get("view");
  const marked = (window.history.state as { termView?: string } | null)?.termView;
  const view = detail ? "workspace" : phone ? "overview" : asked === "overview" || asked === "workspace" ? asked : marked === "overview" ? "overview" : openingView(loadView(), loadWorkspace());
  if (view === "workspace") return <TerminalWorkspaceScreen detail={detail} query={query} {...rest} />;
  return phone ? <PhoneTerminals {...rest} /> : <DesktopTerminals {...rest} />;
}

function DesktopTerminals({ toast, project, projects }: { toast: (text: string) => void; project: string; projects: Project[] }) {
  // The Overview marks its history entry: Back from a terminal opened here must come back to the
  // cards, not to whatever `/app/terminals` would choose by then (the Workspace, once a pane is set
  // up). In the entry's state rather than its address, so the address stays the one every link uses.
  useEffect(() => {
    try {
      window.history.replaceState({ ...(window.history.state ?? {}), termView: "overview" }, "");
    } catch {
      /* the choice falls back to the remembered one */
    }
  }, []);
  const toWorkspace = () => {
    saveView("workspace");
    navigate(pathFor("terminals", null, { view: "workspace" }));
  };
  const [filter, setFilter] = useState<TerminalFilter>("all");
  const [picking, setPicking] = useState<HTMLElement | null>(null);
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<ReadonlySet<string>>(() => new Set());
  // The host says how often a preview is worth refreshing; the environments come with the listing.
  const [pollMs, setPollMs] = useState(3000);
  const list = useQuery<TerminalList>(LIST_KEY, { pollMs, staleMs: 1000 });
  const load = useQuery<TerminalLoad>("/api/terminals/load", { pollMs: LOAD_POLL_MS, staleMs: 3000 });
  const envs = list.data?.envs ?? [];
  const wanted = Math.max(1000, envs.find((e) => e.available)?.preview_poll_ms ?? 3000);
  if (wanted !== pollMs) setPollMs(wanted);

  const rows = useMemo(() => list.data?.terminals ?? [], [list.data]);
  const counts = headerCounts(rows);
  const shown = rows.filter((r) => matches(r, filter));
  const groups = groupTerminals(shown, projects);
  const names = useMemo(() => new Map(projects.map((p) => [p.id, p.name])), [projects]);
  const homes = useMemo(() => new Map((list.data?.envs ?? []).map((e) => [e.env, e.home])), [list.data?.envs]);

  const refresh = useCallback(() => {
    invalidate("/api/terminals");
  }, []);

  const remove = async (row: TerminalRow) => {
    try {
      await api.removeTerminal(row.id);
    } catch (error) {
      toast(errorText(error));
    }
    refresh();
  };

  // Clean-up: several terminals at once, each after one question that lists them all.
  const endMany = async (targets: TerminalRow[], title?: string) => {
    const ended = await endTerminals(targets, toast, title);
    refresh();
    return ended;
  };
  const idle = rows.filter(idleUnwatched);
  const finished = rows.filter((r) => !running(r));
  const endIdle = () => {
    if (!idle.length) toast(t("term.bulk.idle.none"));
    else void endMany(idle, plural("term.bulk.idle.title", idle.length));
  };
  const removeFinished = async () => {
    await removeTerminals(finished.map((r) => r.id), toast);
    refresh();
  };
  const toggleSelected = (id: string) => setSelected((all) => {
    const next = new Set(all);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    return next;
  });
  const chosen = rows.filter((r) => selected.has(r.id));
  const endSelected = async () => {
    const ended = await endMany(chosen.filter(running));
    if (ended !== null) {
      setSelected(new Set());
      setSelecting(false);
    }
  };

  // On a phone the chips run past the edge; the row fades there so "Finished" reads as a swipe away.
  const filterRow = useRef<HTMLDivElement>(null);
  useEdgeFade(filterRow, rows.length);
  const subtitle = list.data ? t("term.screen.counts", { open: counts.open, host: counts.host, staff: counts.staff }) : undefined;
  const lens = projects.find((p) => p.id === project) ?? null;

  return (
    <div className="tp tp-overview">
      <PageHeader
        title={screenTitle("terminals")}
        subtitle={subtitle}
        actions={
          <>
          <ViewSwitch view="overview" onChange={(v) => { if (v === "workspace") toWorkspace(); }} />
          {rows.some(running) && (
            <button className={`btn small ${selecting ? "on" : ""}`} aria-pressed={selecting} data-action="select" onClick={() => { setSelecting((on) => !on); setSelected(new Set()); }}>
              <Icon name="check" size={14} />
              <span>{t(selecting ? "term.select.done" : "term.select")}</span>
            </button>
          )}
          {idle.length > 0 && !selecting && (
            <button className="btn small" data-action="end-idle" title={t("term.bulk.idle.hint")} onClick={endIdle}>
              <Icon name="stop" size={14} />
              <span>{t("term.bulk.idle")}</span>
              <span className="num">{idle.length}</span>
            </button>
          )}
          {filter === "finished" && finished.length > 0 && !selecting && (
            <button className="btn small" data-action="remove-finished" onClick={() => void removeFinished()}>
              <Icon name="trash" size={14} />
              <span>{t("term.bulk.finished")}</span>
            </button>
          )}
          <button className="btn primary small term-new-button" onClick={(e) => setPicking(e.currentTarget)} aria-haspopup="menu" aria-expanded={!!picking} disabled={list.data ? !envs.some((e) => e.available) : false}>
            <Icon name="plus" size={16} />
            <span>{t("term.new")}</span>
          </button>
          </>
        }
      >
        {load.data && <LoadBar load={load.data} compact />}
        {selecting && (
          <div className="term-select-bar" role="toolbar" aria-label={t("term.select")}>
            <span className="sub num">{t("term.select.n", { n: chosen.length })}</span>
            <button className="btn small danger" data-action="end-selected" disabled={!chosen.some(running)} onClick={() => void endSelected()}>
              <Icon name="stop" size={14} />
              <span>{t("term.select.end")}</span>
            </button>
          </div>
        )}
        <div ref={filterRow} className="chips term-filters" role="group" aria-label={t("term.filter.label")}>
          {FILTERS.map((f) => {
            const n = rows.filter((r) => matches(r, f)).length;
            return (
              <button key={f} className="chip select" aria-pressed={filter === f} data-filter={f} onClick={() => setFilter(f)}>
                {t(`term.filter.${f}`)}
                {n > 0 && <span className="num"> · {n}</span>}
              </button>
            );
          })}
        </div>
      </PageHeader>
      <div className="screen wide term-screen-list">
        {list.loading && !list.error && <Skeleton rows={3} />}
        {list.error && !list.data && (
          <div className="empty">
            <b>{t("term.screen.error")}</b>
            <div>{list.error}</div>
            <button className="btn primary" onClick={() => void list.refresh()}>{t("common.retry")}</button>
          </div>
        )}
        {list.data && rows.length === 0 && (
          <div className="empty">
            <b>{t("term.screen.empty")}</b>
            <div>{t("term.screen.empty.sub")}</div>
          </div>
        )}
        {list.data && rows.length > 0 && shown.length === 0 && <div className="empty"><b>{t("term.screen.none")}</b></div>}
        {groups.map((group) => (
          <section key={group.key || "none"} className="term-group" data-group={group.key || "none"}>
            <div className="section-title term-group-title">
              <span>{group.name ?? t("term.screen.noproject")}</span>
              <span className="n">{group.rows.length}</span>
              {group.rows.filter(running).length > 1 && !selecting && (
                <button className="btn small ghost term-group-end" data-action="end-group" onClick={() => void endMany(group.rows.filter(running), plural("term.bulk.group.title", group.rows.filter(running).length, { name: group.name ?? t("term.screen.noproject") }))}>
                  <Icon name="stop" size={14} />
                  <span>{t("term.bulk.group")}</span>
                </button>
              )}
            </div>
            <div className="term-cards">
              {group.rows.map((row) => (
                <TerminalCard key={row.id} row={row} projectName={row.project_id ? names.get(row.project_id) ?? null : null} home={homes.get(row.env) ?? null} onRemove={() => void remove(row)} toast={toast}
                  selection={selecting && running(row) ? { on: selected.has(row.id), toggle: () => toggleSelected(row.id) } : null} />
              ))}
            </div>
          </section>
        ))}
      </div>
      {picking && <FreeTerminalPicker envs={envs} lens={lens} projects={projects} toast={toast} anchor={picking} phone={false} onClose={() => setPicking(null)} />}
    </div>
  );
}

function TerminalCard({ row, projectName, home, onRemove, toast, selection }: { row: TerminalRow; projectName: string | null; home: string | null; onRemove: () => void; toast: (text: string) => void; selection: { on: boolean; toggle: () => void } | null }) {
  const status = cardStatus(row);
  const live = running(row);
  const open = () => navigate(terminalPath(row.id));
  const action = live && row.activity?.action;
  const card = useRef<HTMLElement>(null);
  const stale = staleFor(row);
  // Rename and End are the list row's own commands, with the same sheet and the same questions.
  const commands = useTerminalCommands(row, toast);
  const rename = commands.items[0];
  useContextActions(card, [
    { label:t("term.card.open"), icon:"terminal", onSelect:open },
    ...(action ? [{ label:action.kind === "answer" ? t("term.card.answer") : action.label, onSelect:() => navigate(action.path) }] : []),
    ...(rename !== "-" ? [rename] : []),
    ...(live ? ["-" as const, { label:t("term.end"), icon:"stop" as const, danger:true, onSelect:() => void commands.end() }] : [{ label:t("term.remove"), icon:"trash" as const, onSelect:onRemove }]),
  ]);
  return (
    <article ref={card} className={`term-card ${row.env === "host" ? "host" : ""} ${live ? "" : "finished"} ${stale ? "stale" : ""} ${selection ? "selecting" : ""} ${selection?.on ? "selected" : ""}`} data-terminal={row.id} data-stale={stale ? "1" : undefined}>
      {selection && (
        <label className="term-card-check" title={t("term.select.one", { title: row.title || t("term.untitled") })}>
          <input type="checkbox" checked={selection.on} onChange={selection.toggle} aria-label={t("term.select.one", { title: row.title || t("term.untitled") })} />
        </label>
      )}
      <button className="term-card-open" onClick={selection ? selection.toggle : open} aria-label={t("term.open", { title: row.title })}>
        <div className="term-card-head">
          <span className="term-card-title truncate">{row.title || t("term.untitled")}</span>
          <EnvPill env={row.env} />
        </div>
        <div className="term-card-owner sub truncate">{ownerLine(row, projectName)}</div>
        <pre className="term-card-preview" aria-hidden="true">
          {previewRows(row.preview).map((line, y) => (
            <div key={y} className="term-card-line">
              {line.map((run, x) => (
                <span key={x} style={runStyle(run)}>{run.t}</span>
              ))}
            </div>
          ))}
        </pre>
        {/* The pane's status line, shortened: the agent, the place, the meter, the cost. */}
        {live && <TerminalStatusLine row={row} home={home} variant="card" />}
      </button>
      <div className="term-card-foot">
        <StateDot status={paneState(row)} className="term-card-dot" />
        <span className="term-card-status sub truncate" data-level={stale ? "stale" : status.level} title={stale ? t("term.card.stale", { t: stale }) : undefined}>{status.text}</span>
        {!live ? (
          <button className="btn small ghost" onClick={onRemove}>{t("term.remove")}</button>
        ) : (
          <>
            {/* Ending is beside opening, not behind a menu: a terminal left running for a week is
                cleaned up from here, and the same question as everywhere is asked when anything runs. */}
            <button className="btn small ghost danger term-card-end" data-action="end" onClick={() => void commands.end()} title={t("term.close")}>
              <Icon name="stop" size={14} />
              <span>{t("term.end")}</span>
            </button>
            {action ? (
              <button className={`btn small ${row.activity?.level === "warn" || row.activity?.level === "bad" ? "warn" : ""}`} onClick={() => navigate(action.path)}>{action.kind === "answer" ? t("term.card.answer") : action.label}</button>
            ) : (
              <button className="btn small" onClick={open}>{t("term.card.open")}</button>
            )}
          </>
        )}
      </div>
      {commands.layer}
    </article>
  );
}
